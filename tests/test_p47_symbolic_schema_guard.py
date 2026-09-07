# -*- coding: utf-8 -*-
"""P47 符号记忆表自愈。

审计修复部署冒烟 2026-09-07 抓到：`POST /v11/symbolic/offload` 在生产 500
`table symbolic_blocks has no column named owner_principal`——生产表是 v8 时代老形态，
`V14_ADDITIVE_STATEMENTS` 只被从未接线的 migrate_schema_v14 引用；新建库则根本没有
symbolic_* 表。SymbolicMemoryService 构造时自愈：建表 + 守卫式 ALTER 补 owner_principal，
与 graph_edges valid_from/valid_until（v20 判例）同法。
"""
import contextlib
import tempfile
import unittest
from pathlib import Path

from mimir_v8.store import CanonicalStore
from mimir_v8.symbolic_memory import SymbolicMemoryService

LEGACY_BLOCKS = """CREATE TABLE symbolic_blocks (
    block_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, node_id TEXT NOT NULL,
    parent_node_id TEXT, block_type TEXT NOT NULL DEFAULT 'log', summary TEXT NOT NULL,
    raw_text TEXT NOT NULL, mermaid_line TEXT, token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, UNIQUE(session_key, node_id)) STRICT"""
LEGACY_CANVASES = """CREATE TABLE symbolic_canvases (
    canvas_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, mermaid TEXT NOT NULL,
    block_count INTEGER NOT NULL DEFAULT 0, total_token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_key)) STRICT"""


def _cols(store, table):
    with contextlib.closing(store.connect()) as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


class TestSymbolicSchemaGuard(unittest.TestCase):
    def test_fresh_db_gets_tables_and_offload_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            svc = SymbolicMemoryService(store)
            self.assertIn("owner_principal", _cols(store, "symbolic_blocks"))
            block = svc.offload_block(session_key="s1", raw_text="x" * 40, owner_principal="jarvis")
            self.assertEqual(svc.recall_block(block.node_id)["owner_principal"], "jarvis")

    def test_legacy_tables_gain_owner_principal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with store.transaction() as c:
                c.execute("DROP TABLE IF EXISTS symbolic_blocks")
                c.execute("DROP TABLE IF EXISTS symbolic_canvases")
                c.execute(LEGACY_BLOCKS)
                c.execute(LEGACY_CANVASES)
                c.execute("INSERT INTO symbolic_blocks(block_id,session_key,node_id,summary,raw_text,created_at) VALUES('b0','s0','n0','old','old text','2026-08-01T00:00:00+00:00')")
            self.assertNotIn("owner_principal", _cols(store, "symbolic_blocks"))
            svc = SymbolicMemoryService(store)
            self.assertIn("owner_principal", _cols(store, "symbolic_blocks"))
            self.assertIn("owner_principal", _cols(store, "symbolic_canvases"))
            # legacy row survives with the default owner; new writes work
            self.assertEqual(svc.recall_block("n0")["owner_principal"], "")
            block = svc.offload_block(session_key="s0", raw_text="new" * 20, owner_principal="mentor")
            self.assertEqual(svc.recall_block(block.node_id)["owner_principal"], "mentor")
            # idempotent: constructing again is a no-op
            SymbolicMemoryService(store)


if __name__ == "__main__":
    unittest.main()
