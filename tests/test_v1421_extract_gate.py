# -*- coding: utf-8 -*-
"""v14.2.1-1 知识喂料闸门开刀（2902 积压正主）。

实测：stored 积压 rss 2461 + vault 478，其中正文被 retention 清掉的
占大头（rss 78% / vault 92% '[RETAINED CONTENT PURGED]'）。真实可抽
≈586 条。修三面：
1. llm_extract_once 源过滤改可配置：MIMIR_EXTRACT_CATEGORIES 环境变量
   （默认仍 'conversation'——生产行为不变），vault/rss 按需加入
2. 壳 run 清账：purged 内容的 stored run 标 extracted+content_purged
3. purged 防线：抽取 SELECT 排除 purged 正文

TDD RED → GREEN.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore
from mimir_v8 import worker
from mimir_v8.worker import llm_extract_once


def _mk_source(store, *, category, connector, content, run_status="stored"):
    """造一条带单 user 消息的源 + ingestion run。"""
    from mimir_v8.store import new_id, utc_now
    sid, mid, rid = new_id(), new_id(), new_id()
    now = utc_now()
    with store.transaction() as c:
        c.execute(
            "INSERT INTO conversation_sources(source_id, connector_type, connector_id,"
            " source_hash, owner_principal, retention_class, memory_mode, source_category,"
            " started_at, ended_at, ingested_at, metadata_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, connector, "conn1", new_id(), "mentor", "standard", "observe",
             category, now, now, now, "{}"))
        c.execute(
            "INSERT INTO conversation_messages(message_id, source_id, ordinal, role,"
            " principal_id, content_redacted, content_hash, redaction_applied, created_at, metadata_json)"
            " VALUES(?,?,?,?,?,?,?,0,?,\"{}\")",
            (mid, sid, 0, "user", "mentor", content, new_id(), now))
        c.execute(
            "INSERT INTO ingestion_runs(run_id, source_id, status, requested_by,"
            " idempotency_key, request_fingerprint, message_count, started_at)"
            " VALUES(?,?,?,?,?,?,1,?)",
            (rid, sid, run_status, "test", new_id(), new_id(), now))
    return rid


class TestExtractionGate(unittest.TestCase):

    def test_default_gate_conversation_only(self):
        """默认行为不变：rss/vault 源不被抽取（生产安全面）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            _mk_source(store, category="conversation", connector="hermes_cdc",
                       content="请记住 我的偏好是深色主题")
            _mk_source(store, category="external_info", connector="rss",
                       content="An article about retro computing history")
            r = llm_extract_once(store, "service:extract", limit=10)
            # 无 LLM key → evaluator 走 fallback；关键断言：rss 行未入 rows
            # （created/skipped/failed 的 run 都不含 rss run —— 通过
            #  痕迹面判：rss run 仍 stored）
            with store.connect() as c:
                st = dict(c.execute(
                    "SELECT s.connector_type, i.status FROM ingestion_runs i"
                    " JOIN conversation_sources s ON s.source_id=i.source_id"
                ).fetchall())
            self.assertEqual(st.get("rss"), "stored",
                              "默认闸门必须仍然只吃 conversation")

    def test_env_gate_opens_vault(self):
        """MIMIR_EXTRACT_CATEGORIES 扩册后 vault 源入队。"""
        import importlib
        os.environ["MIMIR_EXTRACT_CATEGORIES"] = "conversation,knowledge_doc"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = CanonicalStore(Path(tmp) / "c.db")
                _mk_source(store, category="knowledge_doc", connector="vault",
                           content="这是一篇关于事件溯源设计模式的笔记，值得长期记住")
                # evaluator 无 key → fallback rule-based; 只要 vault run 被
                # 取进处理循环（不管判定结果），status 就不再是 stored
                llm_extract_once(store, "service:extract", limit=10)
                with store.connect() as c:
                    row = c.execute(
                        "SELECT i.status FROM ingestion_runs i"
                        " JOIN conversation_sources s ON s.source_id=i.source_id"
                        " WHERE s.connector_type='vault'").fetchone()
                self.assertNotEqual(row["status"], "stored",
                                    "扩册后 vault 源必须被抽取循环处理")
        finally:
            os.environ.pop("MIMIR_EXTRACT_CATEGORIES", None)

    def test_purged_content_never_enters_llm(self):
        """retention 清过的正文绝不进 LLM（防线）。"""
        import importlib
        os.environ["MIMIR_EXTRACT_CATEGORIES"] = "conversation,knowledge_doc"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = CanonicalStore(Path(tmp) / "c.db")
                rid = _mk_source(store, category="knowledge_doc", connector="vault",
                                content="[RETAINED CONTENT PURGED]")
                llm_extract_once(store, "service:extract", limit=10)
                with store.connect() as c:
                    row = c.execute(
                        "SELECT status, error_code FROM extraction_runs WHERE run_id=?",
                        (rid,)).fetchone()
                # purged run 必须已被诚实清账：extracted + content_purged
                self.assertIsNotNone(row, "purged run 必须留抽取痕迹")
                self.assertEqual(row["error_code"], "content_purged")
        finally:
            os.environ.pop("MIMIR_EXTRACT_CATEGORIES", None)


if __name__ == "__main__":
    unittest.main()
