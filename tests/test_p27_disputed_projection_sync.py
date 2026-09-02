"""Mímir v12.2.0 — disputed 投影同步闭环 (spec 阶段二任务5).

修复 `status='disputed'` 事实在 FTS/Graph/Vector 投影中的同步逻辑，
彻底消除 verify 一致性门禁误报。

Root cause (closed): ConflictService._mark_disputed wrote a bare
memory_events INSERT with NO outbox rows — the `fact.conflict_lost`
event never entered any projector stream, so all four projectors kept
serving the loser as though it were still active. The projector apply()
paths were already correct (non-active ⇒ remove from projection); the
bug was purely upstream event dispatch.

Fix shape: `_mark_disputed` must fan the event out to every projector in
schema.PROJECTORS, mirroring the store's own fact-event convention
(store._insert_version_and_side_effects → `for projector in PROJECTORS`).

TDD RED → GREEN.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.conflict import ConflictService
from mimir_v8.core_memory import CoreMemoryProjector
from mimir_v8.graph_projector import GraphProjector
from mimir_v8.operations import rebuild_sqlite_projections
from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.schema import CreateFact, PROJECTORS
from mimir_v8.store import CanonicalStore


def _seed(store, content, *, domain="knowledge", owner="mentor"):
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain=domain,
            fact_type="event",
            visibility="all",
            sensitivity="internal",
            egress_policy="external_only" if False else "local_only",
            human_status="confirmed",
        ),
        actor_principal=owner,
    )["fact_id"]


class DisputedProjectionFixture:
    """Seeds two near-duplicate facts, detects, resolves — loser disputed."""

    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.conflict = ConflictService(self.store)
        self.a_id = _seed(self.store, "节点 A 的排障记录 防火墙规则错误 连接被拒绝")
        self.b_id = _seed(self.store, "节点 B 的排障记录 防火墙规则错误 连接被拒绝")
        report = self.conflict.detect(threshold=0.5)
        assert report["created"] == 1, report
        conflicts = self.conflict.list()
        self.conflict_id = conflicts[0]["conflict_id"]

    def resolve_winner_a(self):
        return self.conflict.resolve(
            self.conflict_id, winner_fact_id=self.a_id,
            reason="A is canonical", actor_principal="mentor",
        )

    def drain(self, projector, *, limit=200):
        runner = ProjectorRunner(self.store, projector)
        total = 0
        while True:
            result = runner.run_once(limit=limit)
            total += result["processed"]
            if result["processed"] == 0 or result["failed"]:
                break
        return total

    def fts_ids(self, projector):
        import contextlib
        with contextlib.closing(projector.connect()) as connection:
            rows = connection.execute(
                "SELECT fact_id FROM projected_facts WHERE status='active'"
            ).fetchall()
        return {row["fact_id"] for row in rows}

    def graph_nodes(self, projector):
        import contextlib
        with contextlib.closing(projector.connect()) as connection:
            rows = connection.execute("SELECT fact_id FROM fact_nodes").fetchall()
        return {row["fact_id"] for row in rows}


class TestDisputedSyncsToProjections(unittest.TestCase):
    """Resolve 冲突后，四投影流的 outbox 必须出现 conflict_lost 事件投递。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = DisputedProjectionFixture(Path(self._tmp.name))
        self.proj_dir = Path(self._tmp.name) / "proj"

    def tearDown(self):
        self._tmp.cleanup()

    def _outbox_pending_for(self, event_type):
        import contextlib
        with contextlib.closing(self.fx.store.connect()) as connection:
            rows = connection.execute(
                """SELECT o.projector_name FROM outbox o
                JOIN memory_events e ON e.event_seq=o.event_seq
                WHERE e.event_type=? ORDER BY o.projector_name""",
                (event_type,),
            ).fetchall()
        return [row["projector_name"] for row in rows]

    def test_conflict_lost_event_reaches_all_projector_outboxes(self):
        self.fx.resolve_winner_a()
        names = self._outbox_pending_for("fact.conflict_lost")
        self.assertEqual(names, sorted(PROJECTORS))

    def test_fts_projection_drops_disputed_loser(self):
        # 两段式：先把两条事实都投影进去（证 winner 侧在索引里），
        # 再 resolve → drain，断言 loser 被摘除（防假绿：从未投影过的
        # 空索引上 assertNotIn 恒真）。
        fts = FTSProjector(self.proj_dir / "fts.db")
        self.fx.drain(fts)
        seeded = self.fx.fts_ids(fts)
        self.assertIn(self.fx.b_id, seeded)
        self.fx.resolve_winner_a()
        self.fx.drain(fts)
        active = self.fx.fts_ids(fts)
        self.assertIn(self.fx.a_id, active)
        self.assertNotIn(self.fx.b_id, active)

    def test_graph_projection_drops_disputed_loser(self):
        graph = GraphProjector(self.fx.store, self.proj_dir / "graph.db")
        self.fx.drain(graph)
        seeded = self.fx.graph_nodes(graph)
        self.assertIn(self.fx.b_id, seeded)
        self.fx.resolve_winner_a()
        self.fx.drain(graph)
        nodes = self.fx.graph_nodes(graph)
        self.assertIn(self.fx.a_id, nodes)
        self.assertNotIn(self.fx.b_id, nodes)

    def test_core_memory_projection_drops_disputed_loser(self):
        import contextlib
        core = CoreMemoryProjector(self.fx.store, self.proj_dir / "core_memory.db")
        self._promote_both_to_core_memory()
        self.fx.drain(core)
        with contextlib.closing(core.connect()) as connection:
            seeded = {r["fact_id"] for r in connection.execute(
                "SELECT fact_id FROM projected_core_memory").fetchall()}
        self.assertIn(self.fx.b_id, seeded)
        self.fx.resolve_winner_a()
        self.fx.drain(core)
        with contextlib.closing(core.connect()) as connection:
            ids = {r["fact_id"] for r in connection.execute(
                "SELECT fact_id FROM projected_core_memory").fetchall()}
        self.assertIn(self.fx.a_id, ids)
        self.assertNotIn(self.fx.b_id, ids)

    def _promote_both_to_core_memory(self):
        from mimir_v8.core_memory import CoreMemoryService, PromoteCoreMemory

        service = CoreMemoryService(self.fx.store)
        for position, fact_id in enumerate((self.fx.a_id, self.fx.b_id)):
            service.promote(
                PromoteCoreMemory(
                    agent_id="mentor", block_name="key_decisions",
                    fact_id=fact_id, reason="disputed projection test",
                    idempotency_key=f"p27-promote-{position}",
                    position=position,
                ),
                actor_principal="mentor",
            )

    def test_vector_projection_drops_disputed_loser(self):
        from mimir_v8.vector_projector import VectorProjector

        class FakeCollection:
            def __init__(self):
                self.items = {}
            def upsert(self, ids, embeddings, documents, metadatas):
                for i, fid in enumerate(ids):
                    self.items[fid] = {
                        "vector": embeddings[i], "document": documents[i],
                        "metadata": metadatas[i],
                    }
            def get(self, ids, include=None):
                return {"ids": [fid for fid in ids if fid in self.items]}
            def delete(self, ids):
                for fid in ids:
                    self.items.pop(fid, None)
            def count(self):
                return len(self.items)

        collection = FakeCollection()
        vector = VectorProjector(
            collection, lambda text: [0.1, 0.2, 0.3],
            collection_name="mimir_v8_shadow_test",
        )
        self.fx.drain(vector)
        self.assertIn(self.fx.b_id, collection.items)  # 两段式防假绿
        self.fx.resolve_winner_a()
        self.fx.drain(vector)
        self.assertIn(self.fx.a_id, collection.items)
        self.assertNotIn(self.fx.b_id, collection.items)


class TestDisputedRebuildGate(unittest.TestCase):
    """完整 rebuild 门禁：disputed 存在时对拍必须 consistent（verify 误报清零）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = DisputedProjectionFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_rebuild_gate_passes_with_disputed_facts(self):
        self.fx.resolve_winner_a()
        report = rebuild_sqlite_projections(
            self.fx.store.path, Path(self._tmp.name) / "rebuild",
        )
        self.assertTrue(report["consistent"], report)
        self.assertEqual(report["actual"]["fts"], report["expected"]["fts"])
        self.assertEqual(report["actual"]["graph"]["nodes"],
                         report["expected"]["graph"]["nodes"])


if __name__ == "__main__":
    unittest.main()
