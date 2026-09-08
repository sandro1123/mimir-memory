# -*- coding: utf-8 -*-
"""P0-J migrate_cli 续链修（GitHub issue #2）。

病：source=12 被分流到单腿 migrate_schema_v13，只应用 v13 DDL 就盖章
SCHEMA_VERSION=20（审计时 18）——中间 v14~v18 结构从未创建，runtime
按 schema_version 信任库却缺表。

修：12 与其他版本一律走 migrate_schema() 全链（_additive_chain 按
source_version 顺序补齐 V13→V18 全部 additive 语句）。

RED 验证面：从真 v12 库出发经 CLI migrate → 库必须同时含
opinions(v13)/symbolic_blocks(v14)/quality_metrics(v15)/
conflict_resolutions(v16)/crystal_candidates(v17)/multimodal 表(v18)。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

REQUIRED_TABLES = {
    "opinions", "observations",             # v13
    "symbolic_blocks", "symbolic_canvases", # v14 (representative)
    "search_feedback", "quality_metrics",   # v15
    "conflict_resolutions",                 # v16
    "crystal_candidates",                   # v17
    "fact_assets",                         # v18 (multimodal)
}


def _make_v12_db(path: Path) -> None:
    """造一个合法的 schema=12 前身库。

    真实前提：v12 库已带全套 v10 核心结构（facts/relations/memory_events/
    candidate_facts 等）——迁移链只负责 additive 层（v11 知识表 → v18）。
    夹具用 CanonicalStore 建全套 DDL 后把版本戳降回 12 模拟 v12 实库。
    """
    from mimir_v8.store import CanonicalStore
    store = CanonicalStore(path)          # runtime DDL 全套（v20 形态）
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE schema_meta SET value='12' WHERE key='schema_version'")
        conn.commit()
    # v12 库没有 v13~v18 的表——全部 DROP 制造真实前身形态
    drop_tables = [
        "opinions", "observations", "symbolic_blocks", "symbolic_canvases",
        "symbolic_block_links", "search_feedback", "quality_metrics",
        "conflict_resolutions", "crystal_candidates", "fact_assets",
    ]
    with sqlite3.connect(path) as conn:
        for t in drop_tables:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()


class TestMigrateCliChaining(unittest.TestCase):

    def test_v12_migrates_full_chain_not_v13_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v12.db"
            bak = Path(tmp) / "v12.bak.db"
            _make_v12_db(db)
            # 先确认全链函数本身接受 source=12（这是修复的核心主张）
            from mimir_v8.migration import migrate_schema
            migrate_schema(db, bak)
            conn = sqlite3.connect(db)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            missing = REQUIRED_TABLES - tables
            self.assertEqual(missing, set(), f"全链缺表: {missing}")
            version = sqlite3.connect(db).execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(int(version), 20)

    def test_cli_route_sends_12_to_full_chain(self):
        """CLI 层：source=12 不再分流单腿——直接验证 CLI 输出报表的
        target_version 与后续全链一致（回归 v13 单腿路径被拆除）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v12cli.db"
            bak = Path(tmp) / "v12cli.bak.db"
            _make_v12_db(db)
            r = subprocess.run(
                [sys.executable, "-m", "mimir_v8.migrate_cli", "migrate",
                 "--database", str(db), "--backup", str(bak)],
                capture_output=True, text=True, cwd=str(REPO), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-600:])
            report = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertIn("target_version", report)
            # 修复后：12 → 全链 → 20；v13 单腿只有 opinions/observations
            tables = {row[0] for row in sqlite3.connect(db).execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("crystal_candidates", tables, "CLI 路由仍走 v13 单腿？")
            self.assertIn("conflict_resolutions", tables)


if __name__ == "__main__":
    unittest.main()
