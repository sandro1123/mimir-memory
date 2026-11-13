# -*- coding: utf-8 -*-
"""transaction() 的 BEGIN IMMEDIATE 锁冲突重试。

背景：11-08 审计时在生产目录里直接给 store.py 打了热补丁（旁有
store.py.bak.audit-*），从未进仓库——按 master 重新部署会悄悄丢掉它。
本文件把该行为作为正式契约钉住：只对 "locked" 重试，次数有限，退避递增，
耗尽后原样抛出，非锁错误不重试。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8 import store as store_module
from mimir_v8.store import CanonicalStore


class _FlakyConnection:
    """包一层真连接：前 n 次 BEGIN IMMEDIATE 抛指定错误。"""

    def __init__(self, real, fail_times, message):
        self._real = real
        self.fail_times = fail_times
        self.message = message
        self.begin_calls = 0

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self.begin_calls += 1
            if self.begin_calls <= self.fail_times:
                raise sqlite3.OperationalError(self.message)
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestTransactionLockRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CanonicalStore(Path(self.tmp.name) / "c.db")

    def _run(self, fail_times, message="database is locked"):
        holder = {}
        real_connect = self.store.connect

        def connect():
            holder["conn"] = _FlakyConnection(real_connect(), fail_times, message)
            return holder["conn"]

        sleeps = []
        with mock.patch.object(self.store, "connect", connect), \
             mock.patch.object(store_module.time, "sleep", sleeps.append):
            try:
                with self.store.transaction() as conn:
                    conn.execute("SELECT 1")
                error = None
            except sqlite3.OperationalError as exc:
                error = exc
        return holder["conn"], sleeps, error

    def test_recovers_after_transient_lock(self):
        conn, sleeps, error = self._run(fail_times=2)
        self.assertIsNone(error)
        self.assertEqual(conn.begin_calls, 3)
        self.assertEqual(sleeps, [0.5, 1.0], "退避必须递增：0.5*(n+1)")

    def test_gives_up_after_bounded_attempts_and_reraises(self):
        conn, sleeps, error = self._run(fail_times=99)
        self.assertIsInstance(error, sqlite3.OperationalError)
        self.assertIn("locked", str(error))
        self.assertEqual(conn.begin_calls, 5, "最多尝试 5 次，不得无限重试")
        self.assertEqual(len(sleeps), 4)

    def test_non_lock_error_is_not_retried(self):
        conn, sleeps, error = self._run(fail_times=99, message="disk I/O error")
        self.assertIsInstance(error, sqlite3.OperationalError)
        self.assertEqual(conn.begin_calls, 1, "非 locked 错误一次都不许重试")
        self.assertEqual(sleeps, [])

    def test_commit_still_happens_on_success(self):
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _t(x)")
            conn.execute("INSERT INTO _t VALUES (1)")
        check = self.store.connect()
        try:
            self.assertEqual(check.execute("SELECT count(*) FROM _t").fetchone()[0], 1)
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
