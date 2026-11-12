# -*- coding: utf-8 -*-
"""decay_scan 事件写入 — request_id / correlation_id 补齐 + 版本链正确。

回归背景（2026-11-10 舰队审计）：`INSERT INTO memory_events` 漏写两个
NOT NULL 列，且 `current_version` 从未被查询，导致每轮 decay-scan 产生
23 条 IntegrityError、unit exit 1；即便不报错，所有 `fact.decayed` 事件的
aggregate_version 也恒为 1，事件溯源版本链错误。

TDD RED → GREEN.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore
from mimir_v8.worker import decay_scan


class DecayScanEventTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_stale_fact(self, days_ago: int) -> dict:
        fact = self.store.create_fact(
            CreateFact(
                content=f"stale fact {days_ago}",
                summary="s",
                owner_principal="mentor",
                domain="knowledge",
                fact_type="event",
                visibility="all",
                sensitivity="internal",
                egress_policy="local_only",
                human_status="confirmed",
            ),
            actor_principal="mentor",
        )
        stale = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        with contextlib.closing(self.store.connect()) as connection:
            connection.execute(
                "UPDATE facts SET updated_at=? WHERE fact_id=?", (stale, fact["fact_id"])
            )
            connection.commit()
        return fact

    def test_decay_scan_writes_complete_event(self):
        fact = self._seed_stale_fact(days_ago=400)

        report = decay_scan(self.store, actor_principal="service:decay_worker")

        self.assertEqual(report["errors"], [])
        self.assertEqual(report["marked"], 1)

        with contextlib.closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT request_id, correlation_id, aggregate_version FROM memory_events"
                " WHERE event_type='fact.decayed' AND aggregate_id=?",
                (fact["fact_id"],),
            ).fetchone()
            status = connection.execute(
                "SELECT status FROM facts WHERE fact_id=?", (fact["fact_id"],)
            ).fetchone()["status"]

        self.assertIsNotNone(row)
        self.assertTrue(row["request_id"])
        self.assertEqual(row["request_id"], row["correlation_id"])
        self.assertEqual(row["aggregate_version"], fact["version"])
        self.assertEqual(status, "archived")

    def test_decay_scan_ignores_fresh_facts(self):
        self._seed_stale_fact(days_ago=1)
        report = decay_scan(self.store)
        self.assertEqual(report["marked"], 0)
        self.assertEqual(report["errors"], [])


if __name__ == "__main__":
    unittest.main()
