# -*- coding: utf-8 -*-
"""1.0-C1 tombstone 恢复（The Trust Baseline 线C 第一件）。

现状：tombstone=软删（status 翻转+fact.tombstoned 事件），但无快照层
——历史 garbage 后事实全字段与证据链无从恢复；「可悔」承诺只到事件账
本，没有可操作的恢复路径。

三面：
1. tombstone_snapshots 表（守卫式）：tombstone 事务内同步快照 fact 全
   字段（含 current_version 前一版），30 天保留
2. POST /v8/facts/{id}/restore：快照回插+fact.restored 事件+status
   active；幂等（重复 restore 返回已有态）
3. 事件溯源完整：tombstoned→restored 链在 memory_events 可审计

TDD RED → GREEN.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import CreateFact, TombstoneFact
from mimir_v8.store import CanonicalStore, utc_now


def _seed(store, content="生产库只允许经 API 写入", fact_type="iron_rule"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal="mentor",
        fact_type=fact_type, domain="knowledge", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal="mentor")["fact_id"]


class TestTombstoneSnapshot(unittest.TestCase):

    def test_tombstone_writes_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            store.tombstone_fact(TombstoneFact(
                fact_id=fid, reason="误记，待恢复验证",
                expected_version=1, idempotency_key=f"ts-{fid}"),
                actor_principal="mentor")
            with store.connect() as c:
                rows = c.execute(
                    "SELECT fact_id, content, owner_principal FROM tombstone_snapshots"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["fact_id"], fid)
            self.assertIn("生产库", rows[0]["content"])

    def test_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            store.tombstone_fact(TombstoneFact(
                fact_id=fid, reason="oops", expected_version=1,
                idempotency_key=f"ts-{fid}"), actor_principal="mentor")
            result = store.restore_fact(fid, actor_principal="mentor",
                                        reason="恢复验证：误删")
            self.assertEqual(result["status"], "active")
            fact = store.get_fact(fid)
            self.assertEqual(fact["status"], "active")
            # 内容不丢
            self.assertIn("生产库", fact["content"])

    def test_restore_is_event_sourced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            fid = _seed(store)
            store.tombstone_fact(TombstoneFact(
                fact_id=fid, reason="r", expected_version=1,
                idempotency_key=f"ts-{fid}"), actor_principal="mentor")
            store.restore_fact(fid, actor_principal="mentor", reason="undo")
            with store.connect() as c:
                types = [r[0] for r in c.execute(
                    "SELECT event_type FROM memory_events WHERE aggregate_id=?"
                    " ORDER BY aggregate_version", (fid,))]
            self.assertIn("fact.tombstoned", types)
            self.assertIn("fact.restored", types)
            # restored 在 tombstoned 之后
            self.assertLess(types.index("fact.tombstoned"),
                            types.index("fact.restored"))

    def test_restore_nonexistent_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            with self.assertRaises(Exception):
                store.restore_fact("no-such-fact", actor_principal="mentor",
                                   reason="x")

    def test_snapshot_table_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            with store.connect() as c:
                names = [r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertIn("tombstone_snapshots", names)


if __name__ == "__main__":
    unittest.main()
