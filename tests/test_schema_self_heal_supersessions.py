# -*- coding: utf-8 -*-
"""加法表缺失时,运行时必须自愈,而不是拒绝启动。

判例（2026-11-13 实测）：生产库 schema 21、缺 `fact_supersessions`
（#44 引入的加法表,只在 22→23 重建路径里创建）。1.7.0 把它列进
`_assert_required_schema` 的必需集,于是 `CanonicalStore(path)` 直接抛
SchemaVersionError —— 升级即停服。

这与 `_ensure_additive_columns` 是同一类：纯加法、不改行义、不需要
版本号跳变。修法是让打开的路径也负责把这类表补齐（幂等）。
"""
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mimir_v8.store import CanonicalStore

class TestSupersessionTableSelfHeals(unittest.TestCase):
    def test_missing_supersession_table_is_created_on_open(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "c.db"
            CanonicalStore(db)                      # 正常建库
            with contextlib.closing(sqlite3.connect(db)) as c:
                c.execute("DROP TABLE fact_supersessions")   # 模拟生产 1.6.0 形状
                c.commit()
                self.assertIsNone(
                    c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='fact_supersessions'").fetchone(),
                    "前置条件:表必须已不存在",
                )

            store = CanonicalStore(db)              # 不得抛错,且应补齐
            conn = store.connect()
            try:
                self.assertIsNotNone(
                    conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                 "AND name='fact_supersessions'").fetchone(),
                    "打开后 fact_supersessions 必须存在",
                )
                # 索引也应在(7.7 的加法件是表+索引)
                self.assertIsNotNone(
                    conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                                 "AND name='idx_supersede_older'").fetchone(),
                )
            finally:
                conn.close()

    def test_self_heal_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "c.db"
            store = CanonicalStore(db)
            from mimir_v8.schema import CreateFact
            store.create_fact(
                CreateFact(content="c", owner_principal="mentor", domain="system",
                           fact_type="pattern", summary="s", idempotency_key="k1"),
                actor_principal="mentor",
            )
            with contextlib.closing(sqlite3.connect(db)) as c:
                c.execute("DROP TABLE fact_supersessions")
                c.commit()

            store2 = CanonicalStore(db)
            conn = store2.connect()
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1,
                    "自愈不得动既有数据",
                )
            finally:
                conn.close()

if __name__ == "__main__":
    unittest.main()
