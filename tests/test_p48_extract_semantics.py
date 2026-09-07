# -*- coding: utf-8 -*-
"""P48 抽取链 LLM 失败语义化：LLM 不可用 ≠ 对话不值得记。

生产判例（09-07 审计 P1-5 + 09-08 移交单 §2.2）：Evaluator._call_llm 抖动
（9router 7 天 19 次重启）时返回 None → _parse_response 产
parse_error="LLM returned empty response" → decide()=discard →
extraction_runs 记 cancelled 且 ingestion_runs 标 extracted ——该对话
永不再进队，且 cancelled 同时承载「正常 discard / exact_duplicate /
LLM 故障」三种语义，事后不可区分。9router 22:12~22:19 七分钟 12 次
重启窗内的对话全部被静默永久丢弃。

v14.1.1 施工（移交单 §2.2 建议）：
1. EvaluationResult 加 llm_unavailable 标记（_call_llm None/异常路径）。
2. llm_extract_once 遇 llm_unavailable：extraction_runs 记
   status='failed' + error_code='llm_unavailable'，ingestion_runs
   保持 'stored' 供下轮重试。
3. 退避：24h 内同 run failed(llm_unavailable) ≥ LLM_BACKOFF_HITS 次则
   出队时跳过（不再每 30 分钟撞死马）。
4. 返回结构带 llm_unavailable 计数，ops 可观测。

TDD RED → GREEN。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.evaluator import EvaluationResult, Evaluator
from mimir_v8.learning import (
    ConversationEnvelope,
    ConversationMessage,
    LearningService,
)
from mimir_v8.store import CanonicalStore
from mimir_v8.worker import LLM_BACKOFF_HITS, llm_extract_once


def _ingest(store, content="我偏好简洁的答案", owner="mentor", connector="hermes_cdc"):
    learning = LearningService(store)
    env = ConversationEnvelope(
        connector_type=connector, connector_id="test",
        session_id=None, owner_principal=owner,
        memory_mode="observe", retention_class="standard",
        messages=(ConversationMessage(role="user", content=content),),
        source_uri="https://example.com", title="test",
        idempotency_key=f"p48:{new_id()}" if False else f"p48:{content[:8]}:{owner}",
    )
    return learning.ingest_conversation(env, "service:test")


def new_id():
    import uuid
    return str(uuid.uuid4())


class _DeadLLMEvaluator(Evaluator):
    """模拟 9router 抖动：_call_llm 永远返回 None（无 key 时 evaluate 会先走
    规则腿，所以直接覆写 evaluate 出口不合适——覆写 _call_llm 并强制有 key）。"""

    def __init__(self):
        super().__init__(api_key="test-key", api_url="http://127.0.0.1:1")

    def _call_llm(self, messages):
        return None


class TestLLMUnavailableSemantics(unittest.TestCase):
    """LLM 不可用时：failed 可区分、ingestion 保持 stored 可重试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")
        self.ingest = _ingest(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _runs(self):
        with self.store.connect() as conn:
            ex = dict(conn.execute(
                "SELECT status, error_code FROM extraction_runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone() or {})
            ing = conn.execute(
                "SELECT status FROM ingestion_runs WHERE run_id=?",
                (self.ingest["run_id"],),
            ).fetchone()
        return ex, (ing[0] if ing else None)

    def test_dead_llm_marks_result_unavailable(self):
        """死 LLM → EvaluationResult.llm_unavailable=True（不冒充低价值）。"""
        ev = _DeadLLMEvaluator()
        result = ev.evaluate("我偏好简洁的答案")
        self.assertTrue(result.llm_unavailable, "LLM 不可用必须显式标记，不能静默折成低价值")
        self.assertTrue(result.parse_error, "parse_error 兼容面保留（旧消费面不炸）")

    def test_dead_llm_records_failed_and_keeps_stored(self):
        """死 LLM → extraction failed(llm_unavailable)、ingestion 仍 stored。"""
        import mimir_v8.worker as worker_mod
        original = worker_mod.Evaluator
        worker_mod.Evaluator = _DeadLLMEvaluator
        try:
            out = llm_extract_once(self.store, "service:test", limit=10)
        finally:
            worker_mod.Evaluator = original
        ex, ing = self._runs()
        self.assertEqual(ex.get("status"), "failed")
        self.assertEqual(ex.get("error_code"), "llm_unavailable")
        self.assertEqual(ing, "stored", "LLM 故障不得消费 ingestion 位（否则永不再重试）")
        self.assertEqual(out["evaluator_mode"], "llm")
        self.assertGreaterEqual(out.get("llm_unavailable", 0), 1)

    def test_backoff_skips_run_after_repeated_failures(self):
        """同 run 24h 内 failed(llm_unavailable) 达 LLM_BACKOFF_HITS → 出队跳过。"""
        import mimir_v8.worker as worker_mod
        original = worker_mod.Evaluator
        worker_mod.Evaluator = _DeadLLMEvaluator
        try:
            for _ in range(LLM_BACKOFF_HITS):
                llm_extract_once(self.store, "service:test", limit=10)
            out = llm_extract_once(self.store, "service:test", limit=10)
        finally:
            worker_mod.Evaluator = original
        # 第 BACKOFF_HITS+1 轮：run 已被退避排除 → 不再评估、不再写 failed 行
        with self.store.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM extraction_runs WHERE error_code='llm_unavailable'"
            ).fetchone()[0]
        self.assertEqual(n, LLM_BACKOFF_HITS, "退避生效后不得再累积 failed 行")
        # 出队为空 → no_data 模式（退避把队列排干净了，正是设计行为）
        self.assertEqual(out["evaluator_mode"], "no_data")


class TestHealthyPathUnchanged(unittest.TestCase):
    """健康 LLM / 规则腿：既有行为零回归。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")
        self.ingest = _ingest(self.store, content="随便聊聊天气")

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_data_mode_unchanged(self):
        out = llm_extract_once(self.store, "service:test", limit=10)
        # 无 key → evaluate 走规则腿；无 marker → discard=cancelled（既有语义）
        self.assertEqual(out["evaluator_mode"], "llm")
        ex = None
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT status, error_code FROM extraction_runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            ex = dict(row) if row else {}
            ing = conn.execute(
                "SELECT status FROM ingestion_runs WHERE run_id=?",
                (self.ingest["run_id"],),
            ).fetchone()
        # 规则腿判低价值 → cancelled + extracted（正常 discard，非故障）
        self.assertEqual(ex.get("status"), "cancelled")
        self.assertIsNone(ex.get("error_code"), "正常 discard 不得冒 llm_unavailable 之名")
        self.assertEqual(ing[0], "extracted")

    def test_result_default_not_unavailable(self):
        r = EvaluationResult(
            content="x", salience=0.5, risk="low", domain="personal",
            fact_type="user_pref", summary="s", reasoning="r", is_valuable=True,
        )
        self.assertFalse(r.llm_unavailable)


if __name__ == "__main__":
    unittest.main()


class TestSymbolicOffloadEmptyTextRejected(unittest.TestCase):
    """B 卡（移交单 §2.4）：offload 空 raw_text 必 422，不再产生空块。

    生产实证（09-08 00:0x 冒烟）：POST {} → 200 落空块 sym_10f7f8e6
    （raw_len=0）+ 空 canvas——垃圾数据无入口校验。生产残留已清。
    """

    def setUp(self):
        import hashlib
        import json
        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.extraction import ExtractionService
        from mimir_v8.query import QueryKernel
        from fastapi.testclient import TestClient
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canonical.db")
        self.token = f"tok-{new_id()}"
        tp = root / "tokens.json"
        tp.write_text(json.dumps({"principals": [{
            "id": "mentor",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            "scopes": ["read", "write"],
            "roles": [], "admin": True,
        }]}), encoding="utf-8")
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(tp),
            query=QueryKernel(self.store),
            extraction=ExtractionService(self.store),
        )
        self.client = TestClient(create_app(context), raise_server_exceptions=False)
        self.hdr = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_raw_text_is_422(self):
        r = self.client.post("/v11/symbolic/offload", headers=self.hdr, json={})
        self.assertEqual(r.status_code, 422, "空 raw_text 必须被拒（422），不得落空块")
        with self.store.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM symbolic_blocks").fetchone()[0]
        self.assertEqual(n, 0, "被拒请求不得写入任何块")

    def test_whitespace_raw_text_is_422(self):
        r = self.client.post("/v11/symbolic/offload", headers=self.hdr,
                             json={"raw_text": "   \n\t "})
        self.assertEqual(r.status_code, 422, "纯空白 raw_text 同属空内容")

    def test_valid_raw_text_still_works(self):
        r = self.client.post("/v11/symbolic/offload", headers=self.hdr,
                             json={"raw_text": "真实会话日志内容" * 5})
        self.assertEqual(r.status_code, 200)
        self.assertIn("block_id", r.json())
