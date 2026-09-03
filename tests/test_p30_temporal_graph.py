"""Mímir v13.0 — 时态知识图谱 TKG (spec 阶段三任务2).

relations 表获得 valid_from / valid_until 时间区间：
- 「某时刻图谱状态」：GET /v13/graph/history?entity_id=xxx&at_timestamp=yyy
  只返回该时刻有效的关系（valid_from<=t 且 (valid_until 为空或 >t)）。
- 跨时间推理：「过去 30 天谁改了什么配置」→ 无 at_timestamp 的全史查询。
- supersede 不删旧边而是关窗：被替代的关系 valid_until=生效时刻，
  替代关系 valid_from=同一时刻——历史不消失，只是不再「现在」。
- 空串 = open interval（永远有效）——与既有老行零迁移兼容
  （additive 列 NOT NULL DEFAULT ''，老库 ALTER 后全部视为永远有效）。

TDD RED → GREEN.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.candidates import (
    CandidateService,
    CreateCandidate,
    ReviewCandidate,
)
from mimir_v8.schema import CreateFact, SCHEMA_VERSION
from mimir_v8.store import CanonicalStore


def _fact(store, content, *, owner="mentor", fact_type="event"):
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain="knowledge",
            fact_type=fact_type,
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status="confirmed",
        ),
        actor_principal=owner,
    )["fact_id"]


class TKGFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.candidates = CandidateService(self.store)

    def seed_supersedes(self, *, owner="mentor"):
        """old fact -> new fact (supersedes), returns (old_id, new_id, ts)."""
        old_id = _fact(self.store, "旧版备份策略：rsync 全量", owner=owner)
        new_id = _fact(self.store, "新版备份策略：增量 + 校验", owner=owner)
        result = self.candidates.create_candidate(
            CreateCandidate(
                content="新版备份策略：增量 + 校验",
                summary="增量备份",
                proposed_owner_principal=owner,
                proposed_domain="knowledge",
                proposed_fact_type="event",
                proposed_visibility="all",
                proposed_sensitivity="internal",
                proposed_egress_policy="local_only",
                supersedes_fact_id=old_id,
                idempotency_key="tkg-sup-1",
            ),
            actor_principal=owner,
        )
        self.candidates.review_candidate(
            ReviewCandidate(
                candidate_id=result["candidate_id"],
                action="approve",
                reason="approved for TKG test",
                idempotency_key="tkg-review-1",
            ),
            reviewer_principal=owner,
        )
        committed = self.candidates.commit_approved(
            result["candidate_id"], owner, "tkg-commit-1"
        )
        return old_id, committed["fact_id"], committed["committed_at"]


class TestTemporalRelations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = TKGFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_schema_version_is_20(self):
        self.assertEqual(SCHEMA_VERSION, 20)

    def test_fresh_db_has_valid_columns(self):
        import contextlib

        with contextlib.closing(self.fx.store.connect()) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(relations)")
            }
        self.assertIn("valid_from", columns)
        self.assertIn("valid_until", columns)

    def test_new_relations_have_time_windows(self):
        import contextlib

        old_id, new_id, ts = self.fx.seed_supersedes()
        with contextlib.closing(self.fx.store.connect()) as connection:
            rows = connection.execute(
                "SELECT relation_type, valid_from, valid_until FROM relations"
                " WHERE source_fact_id IN (?,?) OR target_id IN (?,?)"
                " ORDER BY relation_type",
                (new_id, old_id, new_id, old_id),
            ).fetchall()
        by_type = {row["relation_type"]: row for row in rows}
        self.assertEqual(by_type["supersedes"]["valid_from"], ts)
        self.assertEqual(by_type["supersedes"]["valid_until"], "")
        # the superseded (old) relation gets its window closed
        self.assertEqual(by_type["superseded_by"]["valid_from"], ts)
        self.assertEqual(by_type["superseded_by"]["valid_until"], ts)

    def test_at_timestamp_sees_pre_supersede_state(self):
        old_id, new_id, ts = self.fx.seed_supersedes()
        from mimir_v8.graph_projector import GraphProjector

        graph = GraphProjector(self.fx.store, Path(self._tmp.name) / "graph.db")
        from mimir_v8.projector import ProjectorRunner

        runner = ProjectorRunner(self.fx.store, graph)
        while runner.run_once(limit=100)["processed"]:
            pass

        # before the supersede moment: neither edge exists yet
        before = graph.history(old_id, at_timestamp="2001-01-01T00:00:00+00:00")
        self.assertEqual(before, [])
        # full history from the old fact's viewpoint: two edges
        # (it is superseded_by the new fact, and the new fact supersedes it)
        after = graph.history(old_id)
        self.assertEqual(len(after), 2)
        types = {edge["relation_type"] for edge in after}
        self.assertEqual(types, {"superseded_by", "supersedes"})
        # far future: only the open-window supersedes edge remains visible
        far = graph.history(old_id, at_timestamp="2099-01-01T00:00:00+00:00")
        self.assertEqual([edge["relation_type"] for edge in far], ["supersedes"])


class TestGraphHistoryAPI(unittest.TestCase):
    """GET /v13/graph/history?entity_id=&at_timestamp= — REST 面。"""

    def setUp(self):
        import hashlib
        import json

        from fastapi.testclient import TestClient

        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.graph_projector import GraphProjector
        from mimir_v8.projector import ProjectorRunner
        from mimir_v8.query import QueryKernel

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.fx = TKGFixture(root)
        self.old_id, self.new_id, self.ts = self.fx.seed_supersedes()
        self.graph = GraphProjector(self.fx.store, root / "graph.db")
        runner = ProjectorRunner(self.fx.store, self.graph)
        while runner.run_once(limit=100)["processed"]:
            pass
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [{
                    "id": "mentor",
                    "token_sha256": hashlib.sha256(b"tok-mentor").hexdigest(),
                    "scopes": ["read", "write", "ingest"],
                    "roles": [],
                    "admin": False,
                }]
            }),
            encoding="utf-8",
        )
        self.client = TestClient(
            create_app(
                ServiceContext(
                    store=self.fx.store,
                    token_store=TokenStore(token_path),
                    query=QueryKernel(self.fx.store),
                    graph=self.graph,
                )
            ),
            raise_server_exceptions=False,
        )
        self.headers = {"Authorization": "Bearer tok-mentor"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_history_endpoint_returns_edges(self):
        r = self.client.get(
            f"/v13/graph/history?entity_id={self.old_id}",
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["entity_id"], self.old_id)
        # both directions touching the old fact are part of its history
        self.assertEqual(len(data["edges"]), 2)
        self.assertEqual(
            {edge["relation_type"] for edge in data["edges"]},
            {"superseded_by", "supersedes"},
        )
        self.assertIsNone(data["at_timestamp"])

    def test_history_at_timestamp_filters(self):
        # before the supersede: nothing visible ('+' must be %2B-encoded,
        # otherwise the query string decodes it to a space)
        r = self.client.get(
            f"/v13/graph/history?entity_id={self.old_id}"
            "&at_timestamp=2001-01-01T00:00:00%2B00:00",
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["edges"], [])

    def test_history_requires_authentication(self):
        r = self.client.get(f"/v13/graph/history?entity_id={self.old_id}")
        self.assertEqual(r.status_code, 401, r.text)


class TestMigrationV19ToV20(unittest.TestCase):
    """v19 库 ALTER 到 v20：老行视为永远有效（空串窗口）。"""

    def test_migrate_v19_to_v20_adds_columns_and_defaults(self):
        import contextlib

        from mimir_v8.migration import migrate_schema

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # build a v19-era database: create fresh (has the v20 columns),
            # then strip them back off and downgrade the version stamp —
            # that reproduces what a real schema-19 relations table looks like
            store = CanonicalStore(root / "canonical.db")
            old_id, new_id, _ = self.fx_seed(store)
            with contextlib.closing(store.connect()) as connection:
                connection.execute("ALTER TABLE relations DROP COLUMN valid_from")
                connection.execute("ALTER TABLE relations DROP COLUMN valid_until")
                connection.execute(
                    "UPDATE schema_meta SET value='19' WHERE key='schema_version'"
                )
                connection.commit()
            report = migrate_schema(root / "canonical.db", root / "backup.db")
            self.assertEqual(report.target_version, 20)
            with contextlib.closing(store.connect()) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(relations)")
                }
                self.assertIn("valid_from", columns)
                self.assertIn("valid_until", columns)
                windows = connection.execute(
                    "SELECT valid_from, valid_until FROM relations"
                ).fetchall()
            for row in windows:
                self.assertEqual(row["valid_from"], "")
                self.assertEqual(row["valid_until"], "")
            # migrating an already-20 db is rejected
            with self.assertRaises(Exception):
                migrate_schema(root / "canonical.db", root / "backup2.db")

    @staticmethod
    def fx_seed(store):
        fx = TKGFixture(Path(store.path).parent)
        fx.store = store
        fx.candidates = CandidateService(store)
        return fx.seed_supersedes()


if __name__ == "__main__":
    unittest.main()
