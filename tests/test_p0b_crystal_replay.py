# -*- coding: utf-8 -*-
"""P0-B 治理/结晶欠账重放：crystallize 失败记账+恢复后自动补跑。

v14.2 P0（09-08 用户拍板 GO）。P48 只闭环抽取链（extraction_runs
已有 status=failed/error_code 语义）；治理腿已有 review_requeue 退避；
结晶腿（crystal scan 00:30）此前零记录——mimir.service 掉线时 timer
直接不跑、scan 异常时只有 journal 留痕，断供期间欠账无从对证，
恢复后只是「碰巧被 7 天窗口盖住」。

设计（对齐 P48/extraction_runs 先例）：
1. crystal_runs 账本：每次 scan 起手 started，成功 completed，
   异常 failed+error_code——失败可证、可数、可查。
2. catchup_replay：起跑时若发现「failed 且其后无 completed」的欠账，
   记 catchup 计数入返回值（systemd journal 可见），scan 本身
   upsert 幂等——补跑即重放，零特殊路径。

TDD RED → GREEN.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.crystallize import CrystalService, V17_ADDITIVE_STATEMENTS
from mimir_v8.schema import CreateFact, SCHEMA_VERSION
from mimir_v8.store import CanonicalStore


def _seed_facts(store, n=3, content_prefix="mimir crystallize replay test"):
    for i in range(n):
        store.create_fact(CreateFact(
            content=f"{content_prefix} {i}", summary=f"sum {i}",
            owner_principal="mentor", fact_type="reference",
            domain="knowledge", visibility="all", sensitivity="internal",
            egress_policy="local_only", human_status="confirmed",
        ), actor_principal="mentor")


class _StoreForcedFailure(CanonicalStore):
    """子类：transaction() 强制抛异常，模拟断供期库不可用。"""

    def __init__(self, *a, fail_transaction=False, **kw):
        super().__init__(*a, **kw)
        self._fail_transaction = fail_transaction
        self.scan_calls = 0

    def transaction(self):
        self.scan_calls += 1
        if self._fail_transaction and self.scan_calls >= 2:
            # 只炸主 scan 事务（第 2 个 transaction）：账本事务（_open_run）
            # 必须能写，否则失败本身成了新的无痕缺口。
            raise RuntimeError("db locked during outage (simulated)")
        return super().transaction()


class TestCrystalRunsLedger(unittest.TestCase):
    """crystal_runs 账本三面：成功记账/失败记账/欠账补跑。"""

    def _store(self, tmp, **kw):
        return _StoreForcedFailure(Path(tmp) / "canon.db", **kw)

    def test_scan_success_writes_completed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            _seed_facts(store)
            svc = CrystalService(store)
            result = svc.scan()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["runs"]["completed"], 1)
            with store.connect() as c:
                rows = c.execute(
                    "SELECT status, error_code FROM crystal_runs"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "completed")
            self.assertIsNone(rows[0]["error_code"])

    def test_scan_failure_writes_failed_run_with_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp, fail_transaction=True)
            svc = CrystalService(store)
            with self.assertRaises(RuntimeError):
                svc.scan()
            with store.connect() as c:
                rows = c.execute(
                    "SELECT status, error_code FROM crystal_runs"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(rows[0]["error_code"], "RuntimeError")

    def test_recovery_replays_failed_debt(self):
        """断供期失败 → 恢复后下轮 scan 带 catchup 标记。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            _seed_facts(store)
            svc = CrystalService(store)
            # 模拟断供：先手动起一个 failed 账（等同掉线期 scan 失败）
            with store.transaction() as c:
                c.execute(
                    """INSERT INTO crystal_runs(run_id, status, started_at,
                    window_days) VALUES('debt-run-1','failed','2026-09-05T00:30:00+00:00',7)"""
                )
            result = svc.scan()
            # 欠账发现 + 本轮正常完成
            self.assertEqual(result["runs"]["catchup"], 1)
            self.assertEqual(result["runs"]["completed"], 1)
            self.assertEqual(result["status"], "ok")

    def test_no_debt_no_catchup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            _seed_facts(store)
            svc = CrystalService(store)
            svc.scan()  # 正常跑一轮（completed）
            result = svc.scan()  # 再跑一轮：无欠账
            self.assertEqual(result["runs"]["catchup"], 0)
            self.assertEqual(result["runs"]["completed"], 1)
            with store.connect() as c:
                done = c.execute(
                    "SELECT COUNT(*) FROM crystal_runs WHERE status='completed'"
                ).fetchone()[0]
            self.assertEqual(done, 2)

    def test_ledger_table_exists_on_fresh_db(self):
        """守卫式：新库（schema 20 全链迁移后）必须带 crystal_runs。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            with store.connect() as c:
                names = [r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
            self.assertIn("crystal_runs", names)


if __name__ == "__main__":
    unittest.main()
