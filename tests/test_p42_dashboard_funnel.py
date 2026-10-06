# -*- coding: utf-8 -*-
"""P42 ③-2 漏斗面板：dashboard 窄代理 POST /api/query/trace + 前端 verdict 渲染。

背景（2026-10-04）：
- 核心 API 昨发 `POST /v8/query?trace=true`，返回 `recall_trace`（六阶段，
  每阶段带 verdict ∈ found|not_found|degraded）；
- 旧 `/api/search/trace`（v12 dedup 漏斗）继续供「今天页·问我的助手」
  （index.html:askAssistant），本卡**不许动**；
- 「检索洞察」漏斗面板改走新代理，阶段卡片渲染新结构：
  字段从 `hit/total/dropped` 变为 `hits/verdict`，degraded 必须可见。

纪律：诚实判语 —— 每阶段必须显示自己的 verdict；degraded 视觉上是红的，
绝不允许只显示命中数（hits-only）把降级藏起来。

TDD RED → GREEN。
"""
import asyncio
import importlib.util
import json
import re
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "dashboard" / "backend" / "main.py"
INDEX = ROOT / "dashboard" / "frontend" / "index.html"


def _load():
    spec = importlib.util.spec_from_file_location("dash_main_p42", str(MAIN))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """捕获出站请求；按注入的 responder 返回。"""
    last = None

    def __init__(self, responder=None, **kwargs):
        self.responder = responder or (lambda *a, **k: _FakeResponse(
            200, {"query": "x", "results": [], "recall_trace": {"stages": []}}))
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).last = {"url": url, "headers": headers or {}, "json": json or {}}
        return self.responder(url, headers=headers, json=json)


class TestQueryTraceProxy(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.routes = {r.path: r for r in self.mod.app.routes if hasattr(r, "methods")}

    def test_query_trace_route_registered_post_only(self):
        self.assertIn("/api/query/trace", self.routes)
        self.assertEqual(self.routes["/api/query/trace"].methods, {"POST"})

    def test_frontend_funnel_uses_new_proxy(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("/api/query/trace", html,
                      "漏斗面板必须切到新代理 /api/query/trace")

    def test_frontend_renders_stage_verdict(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("verdict", html, "前端必须渲染每阶段 verdict")


class TestQueryTraceProxyBehaviour(unittest.TestCase):
    """代理语义：转发 URL/payload、原样回传、错误降级为 status=error。"""

    def setUp(self):
        self.mod = _load()
        self.handler = self.mod.api_query_trace

    def _call(self, body, responder=None):
        with mock.patch.object(self.mod.httpx, "AsyncClient",
                               lambda **kw: _FakeAsyncClient(responder, **kw)):
            return _run(self.handler(body))

    def test_forwards_to_v8_query_with_trace_and_limit(self):
        trace = {"skipped": False, "verdict": "found", "degraded": False,
                 "lanes": {"profile": "p"}, "stages": [
                     {"stage": "RelevanceGate", "verdict": "found", "hits": 1,
                      "elapsed_ms": 0.1, "detail": {}}]}
        out = self._call({"text": "预算", "limit": 7},
                         lambda url, **k: _FakeResponse(200, {"recall_trace": trace}))
        sent = _FakeAsyncClient.last
        self.assertIn("/v8/query", sent["url"])
        self.assertIn("trace=true", sent["url"])
        self.assertEqual(sent["json"]["text"], "预算")
        self.assertEqual(sent["json"]["limit"], 7)
        self.assertEqual(sent["json"]["candidate_limit"], 50)
        # 原样回传核心响应（含 recall_trace）
        self.assertEqual(out["recall_trace"], trace)

    def test_missing_text_is_rejected_without_calling_api(self):
        _FakeAsyncClient.last = None
        out = self._call({"limit": 10})
        self.assertEqual(out.get("status"), "error")
        self.assertIsNone(_FakeAsyncClient.last, "空 text 不应出站")

    def test_default_limit_is_10(self):
        self._call({"text": "x"})
        self.assertEqual(_FakeAsyncClient.last["json"]["limit"], 10)

    def test_api_error_status_becomes_error_dict(self):
        out = self._call({"text": "x"},
                         lambda url, **k: _FakeResponse(500, text="boom"))
        self.assertEqual(out.get("status"), "error")
        self.assertIn("boom", out.get("error", ""))

    def test_transport_failure_becomes_error_dict(self):
        def _raise(url, **k):
            raise RuntimeError("connection refused")
        out = self._call({"text": "x"}, _raise)
        self.assertEqual(out.get("status"), "error")
        self.assertIn("connection refused", out.get("error", ""))

    def test_old_search_trace_proxy_untouched(self):
        """旧通道（今天页助手）必须仍在，且仍带 dedup_threshold。"""
        self.assertIn("/api/search/trace", self.routes_())
        src = Path(MAIN).read_text(encoding="utf-8")
        seg = src.split('@app.post("/api/search/trace")', 1)[1].split("@app.", 1)[0]
        self.assertIn("/v12/search/trace", seg)
        self.assertIn("dedup_threshold", seg)

    def routes_(self):
        return {r.path: r for r in self.mod.app.routes if hasattr(r, "methods")}


class TestFunnelTemplateShape(unittest.TestCase):
    """阶段卡片模板必须匹配新结构：hits + verdict，degraded 红、found 绿、
    not_found 灰；不得残留 hit/total/dropped 的旧字段渲染。"""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        start = cls.html.index("召回漏斗")
        end = cls.html.index("跨模型投影预览")
        cls.panel = cls.html[start:end]

    def test_stage_card_uses_hits_field(self):
        self.assertIn("s.hits", self.panel, "新结构命中字段是 hits")

    def test_stage_card_has_no_legacy_fields(self):
        for legacy in ("s.hit ", "s.total", "s.dropped", "s.hit>0", "s.hit/"):
            self.assertNotIn(legacy, self.panel,
                             f"旧字段 {legacy!r} 不得留在这块面板")

    def test_verdict_badge_present_with_three_way_colouring(self):
        self.assertIn("s.verdict", self.panel)
        self.assertRegex(self.panel, r"badge-green",
                         "found 必须绿")
        self.assertRegex(self.panel, r"badge-red",
                         "degraded 必须红")
        self.assertRegex(self.panel, r"badge-gray",
                         "not_found 必须灰")

    def test_degraded_is_visually_distinct(self):
        """顶层 degraded 徽章必须存在且走红色 class。"""
        self.assertIn("degraded", self.panel)
        idx = self.panel.index("degraded")
        window = self.panel[max(0, idx - 400): idx + 400]
        self.assertIn("badge-red", window)

    def test_runTrace_switches_proxy_and_drops_dedup_threshold(self):
        m = re.search(r"async runTrace\(\)\s*\{.*?\n    \},", self.html, re.S)
        self.assertIsNotNone(m, "runTrace 必须存在")
        body = m.group(0)
        # 实质判据是「调用的 URL」与「发出的 body」，注释里的旧通道说明不算：
        self.assertIn("fetchJSON('/api/query/trace'", body,
                      "runTrace 必须调用新代理")
        self.assertNotIn("fetchJSON('/api/search/trace'", body,
                         "runTrace 不得再调用旧代理")
        # 新通道无 dedup_threshold，发出的请求体里不得有它
        req = re.search(r"JSON\.stringify\(\{.*?\}\)", body, re.S)
        self.assertIsNotNone(req, "runTrace 必须发出请求体")
        self.assertNotIn("dedup_threshold", req.group(0))
        self.assertIn("limit", req.group(0))

    def test_ask_assistant_keeps_old_channel(self):
        m = re.search(r"async askAssistant\(\)\s*\{.*?\n    \},", self.html, re.S)
        self.assertIsNotNone(m, "askAssistant 必须存在")
        body = m.group(0)
        self.assertIn("/api/search/trace", body)
        self.assertIn("dedup_threshold", body)


class TestTemplateMatchesRealCorePayload(unittest.TestCase):
    """契约：用真 QueryKernel 产出的 trace 过一遍代理，面板模板读的每个字段
    都必须在真载荷里存在。防止「模板按猜想的形状写」——那会在页面上静默显示
    空白，正好把不诚实藏起来。"""

    def test_template_reads_stages_from_recall_trace_nesting(self):
        """阶段列表必须从 recall_trace.stages 读（不是顶层 stages）。

        真载荷把 stages 嵌在 recall_trace 里；模板若读 traceData.stages
        面板运行时会空白——HTML 源断言抓不到这类形状错，此处钉死。"""
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("traceData.recall_trace.stages", html,
                      "阶段列表必须读 recall_trace.stages 嵌套路径")
        # 反向：不得再有读顶层 traceData.stages 的绑定
        self.assertNotIn("in traceData.stages", html,
                         "顶层 traceData.stages 是旧形状，必须已清除")

    def test_real_trace_payload_satisfies_template_contract(self):
        import sys
        import tempfile
        sys.path.insert(0, str(ROOT))
        from mimir_v8.query import QueryKernel, QueryRequest
        from mimir_v8.schema import CreateFact
        from mimir_v8.store import CanonicalStore

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            store.create_fact(CreateFact(
                content="生产库只允许经 API 写入", summary="铁律",
                owner_principal="mentor", domain="knowledge",
                fact_type="iron_rule", visibility="all",
                sensitivity="internal", egress_policy="local_only",
                human_status="confirmed",
            ), actor_principal="mentor")
            kernel = QueryKernel(store, vector=None, fts=None, graph=None,
                                 embedder=None)
            raw = kernel.search(QueryRequest(
                text="生产库 API 写入", principal_id="mentor", limit=10,
                include_trace=True))

        trace = raw["recall_trace"]
        # 顶层：面板读 recall_verdict / recall_trace.{degraded,skipped,lanes.profile}
        self.assertIn("recall_verdict", raw)
        self.assertIn("degraded", trace)
        self.assertIn("skipped", trace)
        self.assertIn("profile", trace["lanes"])
        # 每阶段：模板读 s.stage / s.verdict / s.hits / s.elapsed_ms / s.detail
        for s in trace["stages"]:
            for field in ("stage", "verdict", "hits", "elapsed_ms", "detail"):
                self.assertIn(field, s, f"stage {s.get('stage')} missing {field}")
            self.assertIn(s["verdict"], ("found", "not_found", "degraded"))

        # 过一遍真代理：fake transport 回放真载荷，输出必须逐字不变
        mod = _load()
        with mock.patch.object(mod.httpx, "AsyncClient",
                               lambda **kw: _FakeAsyncClient(
                                   lambda url, **k: _FakeResponse(200, raw), **kw)):
            out = _run(mod.api_query_trace({"text": "生产库 API 写入", "limit": 10}))
        self.assertEqual(out, raw, "代理必须原样回传核心响应")

    def test_results_rows_carry_score_explanation(self):
        """Top 命中列表读 r.score_explanation.decay_factor / .not_yet_effective。"""
        import sys
        import tempfile
        sys.path.insert(0, str(ROOT))
        from mimir_v8.query import QueryKernel, QueryRequest
        from mimir_v8.schema import CreateFact
        from mimir_v8.store import CanonicalStore

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            store.create_fact(CreateFact(
                content="预算审批走财务系统", summary="流程",
                owner_principal="mentor", domain="knowledge",
                fact_type="iron_rule", visibility="all",
                sensitivity="internal", egress_policy="local_only",
                human_status="confirmed",
            ), actor_principal="mentor")
            kernel = QueryKernel(store, vector=None, fts=None, graph=None,
                                 embedder=None)
            raw = kernel.search(QueryRequest(
                text="预算审批", principal_id="mentor", limit=10,
                include_trace=True))
        self.assertTrue(raw["results"], "真载荷应至少命中一条，契约才有意义")
        for r in raw["results"]:
            self.assertIn("fact_id", r)
            self.assertIn("content", r)
            self.assertIn("score_explanation", r)
            self.assertIn("decay_factor", r["score_explanation"])
            self.assertIn("not_yet_effective", r["score_explanation"])


if __name__ == "__main__":
    unittest.main()
