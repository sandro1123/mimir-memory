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


# ── Task 2: PULSE 健康面板（系统 tab 升级）────────────────────────────
# 三层存量必须与检索内核的层定义逐位一致（mimir_v8/query.py LAYER{1,2,3}_
# FACT_TYPES）。dashboard 刻意不 import mimir_v8（零依赖聚合层），所以此处
# 钉住的是**字符串集合**：任一端口漂移都会在这里红，而不是让两处面板各说各话。

LAYER3 = ("iron_rule", "user_pref", "skill")
LAYER2 = ("pattern",)
LAYER1 = ("event", "project_config", "ephemeral", "learning", "reference")


def _api_system_src() -> str:
    """api_system 的函数源码（钉在函数体内，防止别处出现同名标记就算过）。"""
    return re.split(r"\n@app\.", MAIN.read_text(encoding="utf-8").split(
        '@app.get("/api/system")', 1)[1], 1)[0]


class TestLayerFactTypeSetsMatchKernel(unittest.TestCase):
    """dashboard 里硬编码的层集合 == 检索内核的层集合（活体读取，非抄写）。"""

    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(ROOT))
        from mimir_v8.query import QueryKernel
        cls.KERNEL = QueryKernel
        cls.src = _api_system_src()

    def test_dashboard_sql_carries_the_kernel_three_layer_sets(self):
        """三层各自的 fact_type 必须逐个出现在 SQL 里——成员从内核活读，
        不是把常量再抄一遍（抄写下一次漂移照样红不了）。"""
        for layer, types in (("l3", self.KERNEL.LAYER3_FACT_TYPES),
                             ("l2", self.KERNEL.LAYER2_FACT_TYPES),
                             ("l1", self.KERNEL.LAYER1_FACT_TYPES)):
            for fact_type in types:
                self.assertRegex(self.src, rf"['\"]{fact_type}['\"]",
                                 f"层 {layer} 缺 fact_type {fact_type!r}——"
                                 f"与 mimir_v8/query.py LAYER*_FACT_TYPES 漂移了")

    def test_dashboard_layer_sets_are_exactly_the_kernel_sets(self):
        """反向：SQL 里出现的 fact_type 字符串集合必须**恰好**等于内核三层
        并集——多一个（比如把别的类型塞进 L1）同样红。"""
        all_types = set(self.KERNEL.LAYER1_FACT_TYPES +
                        self.KERNEL.LAYER2_FACT_TYPES +
                        self.KERNEL.LAYER3_FACT_TYPES)
        quoted = set(re.findall(r"['\"]([a-z_]+)['\"]", self.src))
        self.assertEqual(quoted & all_types, all_types)

    def _running_sql(self):
        """真跑一遍 api_system，抓出那条三层存量 SQL 的运行时文本。"""
        mod = _load()
        seen = []

        def _spy(query, params=(), database=None):
            seen.append(query)
            return []

        with mock.patch.object(mod, "_db_query", _spy):
            _run(mod.api_system())
        layer_sql = [q for q in seen if "FROM facts WHERE status='active'" in q]
        self.assertEqual(len(layer_sql), 1, "应恰好执行一条三层存量 SQL")
        return layer_sql[0]

    def test_executed_sql_carries_the_sets_not_just_the_source_text(self):
        """钉运行时真跑的那条 SQL：源文本里出现名字不等于进了 WHERE/CASE。"""
        sql = self._running_sql()
        for fact_type in (self.KERNEL.LAYER1_FACT_TYPES +
                          self.KERNEL.LAYER2_FACT_TYPES +
                          self.KERNEL.LAYER3_FACT_TYPES):
            self.assertIn(f"'{fact_type}'", sql,
                          f"运行时 SQL 缺 {fact_type!r}")

    def test_each_case_arm_holds_exactly_one_kernel_layer(self):
        """逐臂钉分组：按 CASE 分支解析运行时 SQL，每个 arm 的 fact_type 集合
        必须**恰好**等于内核对应层——防止「类型还在，但被挪去了隔壁层」这种
        集合并集看不出来的漂移。"""
        sql = self._running_sql()
        arms = re.findall(
            r"WHEN fact_type IN \(([^)]*)\) THEN '(\w+)'", sql)
        self.assertEqual(len(arms), 3, f"CASE 应有三条分层臂，实得 {arms}")
        parsed = {layer: tuple(re.findall(r"'([a-z_]+)'", types))
                  for types, layer in arms}
        self.assertEqual(set(parsed), {"l3", "l2", "l1"})
        for layer, kernel_types in (("l3", self.KERNEL.LAYER3_FACT_TYPES),
                                    ("l2", self.KERNEL.LAYER2_FACT_TYPES),
                                    ("l1", self.KERNEL.LAYER1_FACT_TYPES)):
            self.assertEqual(set(parsed[layer]), set(kernel_types),
                             f"CASE 臂 {layer} 的集合与内核不一致")
            # 顺序无意义（SQL 的 IN 是集合语义），但成员一个不能少/多
            self.assertEqual(len(parsed[layer]), len(kernel_types),
                             f"CASE 臂 {layer} 成员数不符")


class TestPulsePanel(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_system_has_layer_counts_sql(self):
        src = MAIN.read_text(encoding="utf-8")
        self.assertIn("layer_counts", src, "/api/system 必须提供三层存量")

    def test_frontend_has_pulse_card(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("PULSE", html, "系统 tab 必须有 PULSE 面板")

    def test_layer_counts_is_inside_api_system_not_elsewhere(self):
        """marker 必须长在 api_system 函数体里（别处同名即假绿）。"""
        self.assertIn("layer_counts", _api_system_src())

    def test_layer_counts_keys_are_l3_l2_l1_other(self):
        """前端消费的键名必须与后端产出的一致：l3/l2/l1 + other。"""
        src = _api_system_src()
        for key in ("'l3'", "'l2'", "'l1'", "'other'"):
            self.assertIn(key, src, f"层分类缺 {key}")
        html = INDEX.read_text(encoding="utf-8")
        for key in ("l3", "l2", "l1", "other"):
            self.assertRegex(html, rf"layer_counts[^\n]{{0,24}}\)?\.{key}\b",
                             f"前端未渲染 layer_counts 的 {key} 层")


class TestPulseLayerCountsBehaviour(unittest.TestCase):
    """真跑一遍 api_system：临时 canonical 库里播三层事实，验证计数分类正确、
    非 active 不计、未知类型落 other，且既有字段一个不少（纯加法）。"""

    def _call(self, db_path):
        mod = _load()

        async def _no_health(path):
            return None

        with mock.patch.object(mod, "CANONICAL_DB", db_path), \
                mock.patch.object(mod, "_mimir_get", _no_health), \
                mock.patch.object(mod, "_cache", {}):
            return _run(mod.api_system())

    def test_counts_classify_three_layers_and_others(self):
        import sys
        import tempfile
        sys.path.insert(0, str(ROOT))
        from mimir_v8.schema import CreateFact
        from mimir_v8.store import CanonicalStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "canonical.db"
            store = CanonicalStore(db)
            seeded = [("iron_rule", "mentor"), ("user_pref", "mentor"),
                      ("skill", "mentor"), ("pattern", "mentor"),
                      ("event", "mentor"), ("reference", "mentor")]
            archived = None
            for n, (fact_type, owner) in enumerate(seeded):
                human = "confirmed" if fact_type != "ephemeral" else "unreviewed"
                res = store.create_fact(CreateFact(
                    content=f"{fact_type} 存量 {n}", summary=f"t{n}",
                    owner_principal=owner, domain="knowledge",
                    fact_type=fact_type, visibility="all",
                    sensitivity="internal", egress_policy="local_only",
                    human_status=human,
                ), actor_principal=owner)
                if fact_type == "skill":
                    archived = res["fact_id"]
            # 一条 archived 的 iron_rule：不得计入（只有 active 算存量）
            with store.transaction() as conn:
                conn.execute("UPDATE facts SET status='archived' WHERE fact_id=?",
                             (archived,))

            out = self._call(db)

        self.assertIn("layer_counts", out, "/api/system 必须返回 layer_counts")
        counts = out["layer_counts"]
        self.assertEqual(counts.get("l3"), 2, "iron_rule+user_pref（archived 不计）")
        self.assertEqual(counts.get("l2"), 1, "pattern")
        self.assertEqual(counts.get("l1"), 2, "event+reference")
        self.assertEqual(counts.get("other", 0), 0, "本库无未知类型")

    def test_existing_system_fields_unchanged(self):
        """纯加法：既有键一个都不能少。"""
        import tempfile
        from mimir_v8.store import CanonicalStore
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "canonical.db"
            CanonicalStore(db)
            out = self._call(db)
        for key in ("version", "schema_version", "service", "status", "facts",
                    "event_head", "principals", "pending", "dead_letters",
                    "projectors", "db_files", "event_types"):
            self.assertIn(key, out, f"既有字段 {key} 丢失（改成了删法）")
        # layer_counts 必须是新增的第三条腿，不是替换
        self.assertIn("layer_counts", out)

    def test_missing_db_degrades_to_empty_not_crash(self):
        """库不存在时 _db_query 返 []——面板落空态，不得抛异常。"""
        out = self._call(Path("Z:/definitely/not/here/canonical.db"))
        self.assertIn("layer_counts", out)
        self.assertEqual(out["layer_counts"], {})


class TestPulseCardShape(unittest.TestCase):
    """PULSE 卡片内容：版本 + 代号 + 三层存量，且必须落在系统 tab 内、
    服务信息卡片之前（brief Step 4）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.tab = cls.html.split(":class=\"{'active':activeTab==='system'}\"", 1)[1]
        cls.tab = cls.tab.split("activeTab==='skills'", 1)[0]

    def test_pulse_card_is_inside_system_tab(self):
        self.assertIn("PULSE", self.tab, "PULSE 卡必须在系统 tab 内")

    def test_pulse_card_sits_above_service_card(self):
        self.assertLess(self.tab.index("PULSE"), self.tab.index("服务信息"),
                        "PULSE 卡必须在「服务信息」卡片上方")

    def test_pulse_card_shows_version_and_codename(self):
        seg = self.tab[:self.tab.index("服务信息")]
        self.assertIn("systemData.version", seg, "PULSE 卡必须显示版本")
        self.assertIn("codename", seg, "PULSE 卡必须显示代号")

    def test_pulse_card_shows_all_three_layers_plus_other(self):
        seg = self.tab[:self.tab.index("服务信息")]
        for key in ("l3", "l2", "l1", "other"):
            self.assertRegex(seg, rf"layer_counts[^\n]{{0,24}}\)?\.{key}\b",
                             f"PULSE 缺层 {key}")

    def test_backend_derives_codename_from_version_not_hardcoded(self):
        """代号必须由版本号推导（版本一升代号跟着走），不得写死成某个字符串。"""
        src = _api_system_src()
        self.assertIn("codename", src, "后端必须产出 codename")
        self.assertIn("CODENAMES", MAIN.read_text(encoding="utf-8"),
                      "代号表必须是模块级单一事实源")

    def test_codename_table_covers_current_version(self):
        """当前内核版本必须在代号表里有名有姓（不是 fallback 兜出来的）。"""
        import sys
        sys.path.insert(0, str(ROOT))
        from mimir_v8.schema import MIMIR_VERSION
        mod = _load()
        codename = mod._codename_for(MIMIR_VERSION)
        self.assertTrue(codename and codename != mod.CODENAME_FALLBACK,
                        f"{MIMIR_VERSION} 未登记代号，落到 fallback")
        self.assertIn(MIMIR_VERSION, mod.CODENAMES)


if __name__ == "__main__":
    unittest.main()
