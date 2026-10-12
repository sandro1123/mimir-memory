# -*- coding: utf-8 -*-
"""roadmap #12 + #14：召回解释的机器可读面 + 预算诚实。

TDD RED -> GREEN。

#12 缺口：1.3.0 的 `/v8/query?trace=true` 产出的是**人读**漏斗；
agent 消费的那半边没有——它想知道「为什么命中、以什么代价命中」，
现在只能靠人读日志反推。要一份**定版、带 schema_version、
按声明预算封顶**的 `recall_explanation` 对象。

#14 硬约束：超预算时**禁止**静默把载荷换成 stub 还报 success
（claude-mem #3802/#1899 的失败模式）。必须显式回 `budget_exceeded`
+ 丢弃清单 + 收窄建议。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.query import QueryRequest
from mimir_v8.recall_explanation import RecallExplanation


class TestRecallExplanationValueObject(unittest.TestCase):
    """值对象本身：定版 + 预算声明 + 逐条理由。"""

    def _sample(self, **over):
        base = dict(
            declared_budget=10,
            used=3,
            dropped=[],
            facts=[
                {"fact_id": "f1", "layer": "L3", "channel": "anchor",
                 "rank_reason": "iron_rule anchor injection"},
            ],
        )
        base.update(over)
        return RecallExplanation(**base)

    def test_carries_schema_version(self):
        """#12：对象必须带 schema_version，后续变更走加法不改语义。"""
        obj = self._sample()
        self.assertIsInstance(obj.schema_version, str)
        self.assertTrue(obj.schema_version, "schema_version 不得为空")

    def test_reports_declared_budget_and_usage(self):
        obj = self._sample(declared_budget=10, used=3)
        self.assertEqual(obj.declared_budget, 10)
        self.assertEqual(obj.used, 3)

    def test_not_exceeded_when_within_budget(self):
        obj = self._sample(declared_budget=10, used=3)
        self.assertFalse(obj.budget_exceeded)
        self.assertEqual(obj.dropped, [])

    def test_to_dict_is_json_serializable_and_versioned(self):
        import json
        d = self._sample().to_dict()
        self.assertIn("schema_version", d)
        self.assertIn("budget", d)
        self.assertIn("facts", d)
        json.dumps(d)  # 不得抛异常

    def test_fact_entries_carry_why_hit_and_cost(self):
        """#12 内核：逐条 fact 带来源层/通道/排序理由。"""
        d = self._sample().to_dict()
        fact = d["facts"][0]
        for key in ("fact_id", "layer", "channel", "rank_reason"):
            self.assertIn(key, fact)


class TestBudgetHonesty(unittest.TestCase):
    """#14：超预算必须显式，禁止静默降级还报 success。"""

    def test_exceeded_is_true_and_dropped_enumerated(self):
        obj = RecallExplanation(
            declared_budget=2,
            used=5,
            dropped=[
                {"fact_id": "f3", "reason": "budget_exceeded"},
                {"fact_id": "f4", "reason": "budget_exceeded"},
                {"fact_id": "f5", "reason": "budget_exceeded"},
            ],
        )
        self.assertTrue(obj.budget_exceeded)
        self.assertEqual(len(obj.dropped), 3)

    def test_dropped_entries_must_carry_a_reason(self):
        """丢弃项没有理由 = 无法向调用方解释丢了什么（#14 要求可枚举）。"""
        with self.assertRaises(ValueError):
            RecallExplanation(
                declared_budget=1, used=2,
                dropped=[{"fact_id": "f9"}],  # 缺 reason
            )

    def test_exceeded_without_dropped_list_is_rejected(self):
        """宣称超预算却给不出丢弃清单 —— 正是「静默替换」的形态，禁止。"""
        with self.assertRaises(ValueError):
            RecallExplanation(declared_budget=1, used=2, dropped=[])

    def test_no_silent_stub_state_exists(self):
        """#14 反向不变量：不存在 (成功 且 载荷被换掉) 的组合。

        本对象不提供任何 'success' 字段——成功由调用方从
        budget_exceeded 推导；超预算必然抛错或带丢弃清单。
        """
        d = RecallExplanation(declared_budget=5, used=1).to_dict()
        self.assertNotIn("success", d)
        self.assertIn("budget", d)
        self.assertFalse(d["budget"]["exceeded"])


class TestSearchAttachesExplanation(unittest.TestCase):
    """#12 接线：search() 在 trace 模式挂 recall_explanation，默认不挂。"""

    def _kernel(self):
        import tempfile
        from mimir_v8.projector import FTSProjector
        from mimir_v8.query import QueryKernel
        from mimir_v8.store import CanonicalStore

        tmp = tempfile.mkdtemp()
        # CanonicalStore(path) 自带建库（对齐 test_recall_funnel_trace 用法）
        store = CanonicalStore(Path(tmp) / "canonical.db")
        fts = FTSProjector(Path(tmp) / "fts.db")
        return QueryKernel(store, fts=fts)

    def _seed(self, store, content, fact_type="iron_rule"):
        from mimir_v8.schema import CreateFact
        store.create_fact(
            CreateFact(
                content=content, summary=content[:40], owner_principal="mentor",
                domain="knowledge", fact_type=fact_type,
                visibility="all", sensitivity="internal", egress_policy="local_only",
                confidence_score=0.9, human_status="confirmed",
            ),
            actor_principal="mentor",
        )

    def test_default_response_has_no_explanation(self):
        """默认响应形状必须与从前逐字节一致（不挂 explanation）。"""
        kernel = self._kernel()
        out = kernel.search(QueryRequest(text="probe", principal_id="mentor"))
        self.assertNotIn("recall_explanation", out)

    def test_trace_mode_attaches_explanation_with_schema_version(self):
        kernel = self._kernel()
        self._seed(kernel.store, "iron rule: always cite sources")
        out = kernel.search(QueryRequest(
            text="cite sources", principal_id="mentor", include_trace=True))
        self.assertIn("recall_explanation", out)
        exp = out["recall_explanation"]
        self.assertIn("schema_version", exp)
        self.assertIn("budget", exp)
        self.assertIn("facts", exp)

    def test_explanation_carries_declared_budget(self):
        """预算必须是**声明**的，可从请求推导——不是事后编的。"""
        kernel = self._kernel()
        out = kernel.search(QueryRequest(
            text="probe", principal_id="mentor", limit=7, include_trace=True))
        self.assertEqual(out["recall_explanation"]["budget"]["declared"], 7)

    def test_explanation_agrees_with_human_trace(self):
        """#12 硬要求：人机**共用一份真相**，逐字段对得上。

        recall_explanation（机器）与 recall_trace（人读）必须描述同一次
        召回——不能为前端/agent 各造一套语义。
        """
        kernel = self._kernel()
        self._seed(kernel.store, "iron rule: always cite sources")
        out = kernel.search(QueryRequest(
            text="cite sources", principal_id="mentor", include_trace=True))

        trace = out["recall_trace"]
        exp = out["recall_explanation"]
        # 两者对同一批结果条数一致
        topk = next(s for s in trace["stages"] if s["stage"] == "TopK")
        self.assertEqual(exp["budget"]["used"], topk["hits"])
        # 两者对同一次召回的 verdict 语义一致（都源自同一计算）
        self.assertEqual(len(exp["facts"]), topk["hits"])
        # 逐条 fact_id 与 results 一致
        self.assertEqual(
            [f["fact_id"] for f in exp["facts"]],
            [r["fact_id"] for r in out["results"]],
        )


class TestApiSurface(unittest.TestCase):
    """#12：agent 经 API 消费——POST /v8/query?trace=true 必须挂出
    recall_explanation，且默认（不带 trace）不得挂。

    fixture 克隆自 test_recall_funnel_trace._ApiFixture（同源，免漂移）。
    """

    def setUp(self):
        import hashlib
        import json
        import tempfile
        from fastapi.testclient import TestClient
        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.knowledge import FeedbackLoop, KnowledgeService, UnifiedSearch
        from mimir_v8.query import QueryKernel
        from mimir_v8.store import CanonicalStore

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        knowledge = KnowledgeService(self.store)
        unified = UnifiedSearch(self.query, knowledge,
                                enabled_layers=("memory", "learning"))
        feedback = FeedbackLoop(self.store, knowledge)
        self.token = "p12-api-mentor-token"
        token_path = root / "tokens.json"
        token_path.write_text(json.dumps({"principals": [{
            "id": "mentor",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            "scopes": ["read", "write", "ingest"],
            "roles": [], "admin": False,
        }]}), encoding="utf-8")
        ctx = ServiceContext(
            store=self.store, token_store=TokenStore(token_path), query=self.query,
            knowledge=knowledge, unified_search=unified, feedback_loop=feedback,
        )
        self.client = TestClient(create_app(ctx), raise_server_exceptions=False)

    def tearDown(self):
        self._tmp.cleanup()

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_default_api_response_has_no_explanation(self):
        r = self.client.post("/v8/query", json={"text": "probe", "limit": 5},
                             headers=self._headers())
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("recall_explanation", r.json())

    def test_trace_api_response_has_explanation(self):
        r = self.client.post("/v8/query?trace=true",
                             json={"text": "probe", "limit": 5},
                             headers=self._headers())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("recall_explanation", body)
        self.assertIn("schema_version", body["recall_explanation"])


if __name__ == "__main__":
    unittest.main()
