# -*- coding: utf-8 -*-
"""P47 投影漂移修复。

审计 2026-09-07 P1-1：3 条 08 月 `fact.conflict_lost` 事实在 fts/graph/vector 投影里仍 active，
Mentor 每日「一致性核对」连报 7 天。机理：FTSProjector.apply 的 no-op 守卫只比
version + content_hash——纯状态变更（同版本）被当作「没变」跳过。
修：守卫加 status；operations.reproject_facts 用投影器自己的 apply 重放当前真值。
"""
import contextlib
import tempfile
import unittest
from pathlib import Path

from mimir_v8.graph_projector import GraphProjector
from mimir_v8.operations import reproject_facts
from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class TestReproject(unittest.TestCase):
    def test_status_change_reaches_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CanonicalStore(root / "canonical.db")
            fts = FTSProjector(root / "fts.db")
            graph = GraphProjector(store, root / "graph.db")
            out = store.create_fact(
                CreateFact(content="c", owner_principal="mentor", domain="system",
                           fact_type="pattern", summary="s", idempotency_key="k1"),
                actor_principal="admin",
            )
            fid = out["fact_id"]
            for projector in (fts, graph):
                ProjectorRunner(store, projector).run_once(limit=10)
            with contextlib.closing(fts.connect()) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM facts_fts WHERE fact_id=?", (fid,)).fetchone()[0], 1)

            # simulate the historical drift: canonical flips to disputed without a version bump
            with store.transaction() as c:
                c.execute("UPDATE facts SET status='disputed' WHERE fact_id=?", (fid,))

            report = reproject_facts(store, [fts, graph], [fid, "missing-id"])
            self.assertEqual(report["reprojected"], [fid])
            self.assertEqual(report["missing"], ["missing-id"])
            with contextlib.closing(fts.connect()) as c:
                self.assertEqual(c.execute("SELECT status FROM projected_facts WHERE fact_id=?", (fid,)).fetchone()[0], "disputed")
                self.assertEqual(c.execute("SELECT COUNT(*) FROM facts_fts WHERE fact_id=?", (fid,)).fetchone()[0], 0)
            with contextlib.closing(graph.connect()) as c:
                self.assertIsNone(c.execute("SELECT fact_id FROM fact_nodes WHERE fact_id=?", (fid,)).fetchone())


class TestReprojectCli(unittest.TestCase):
    def test_cli_repairs_and_reports(self):
        import io
        from contextlib import redirect_stdout
        from mimir_v8 import migrate_cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CanonicalStore(root / "canonical.db")
            fts = FTSProjector(root / "fts.db")
            graph = GraphProjector(store, root / "graph.db")
            fid = store.create_fact(
                CreateFact(content="c2", owner_principal="mentor", domain="system",
                           fact_type="pattern", summary="s2", idempotency_key="k2"),
                actor_principal="admin",
            )["fact_id"]
            for projector in (fts, graph):
                ProjectorRunner(store, projector).run_once(limit=10)
            with store.transaction() as c:
                c.execute("UPDATE facts SET status='disputed' WHERE fact_id=?", (fid,))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = migrate_cli.main(["reproject", "--data-dir", str(root), "--fact-ids", f"{fid},nope"])
            self.assertEqual(code, 1)  # 'nope' is missing → non-zero, but repair still ran
            import json
            report = json.loads(buf.getvalue())
            self.assertEqual(report["reprojected"], [fid])
            self.assertEqual(report["missing"], ["nope"])
            self.assertEqual(report["projectors"], ["fts", "graph"])
            with contextlib.closing(fts.connect()) as c:
                self.assertEqual(c.execute("SELECT status FROM projected_facts WHERE fact_id=?", (fid,)).fetchone()[0], "disputed")


if __name__ == "__main__":
    unittest.main()
