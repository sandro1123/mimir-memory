# ③-2 控制台升级（RECALL 漏斗面板 + PULSE 健康面板）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 dashboard 的「检索洞察 → 召回漏斗」面板消费 ③-1 上线的 `/v8/query?trace=true`（六阶段 + 每步 verdict），并把「系统」tab 升级为 PULSE 健康面板（版本/代号/核心模块探针/三层存量与水位）。MAP/SETTINGS/VAULT 不做（B2 决策）。

**Architecture:** 纯 dashboard 侧改动（`dashboard/backend/main.py` + `dashboard/frontend/index.html`）。后端新增一个窄代理 `/api/query/trace`（转发 `MIMIR_API/v8/query?trace=true`，与既有 `/api/search/trace` 同构），前端漏斗面板改调它并渲染 verdict；系统 tab 增补 PULSE 卡片（数据全部来自既有 `/api/system` 已返回字段 + 一个新 `/api/layer_stats` 或就地 SQL）。**不改 mimir_v8 内核**（③-1 已交付）；**不删旧 `/api/search/trace`**（今天页「问我的助手」在用，见 `index.html:1743`）。

**Tech Stack:** FastAPI（dashboard 后端）、Alpine.js + 原生 JS（前端，零构建）、unittest（既有测试风格 `tests/test_p47_dashboard_routes.py`：`importlib` 加载 `main.py` 后断言路由/函数）。

**Spec:** `docs/superpowers/specs/2026-10-04-mimir-aidumei-adoption-design.md`（§三 ③-2）

## Global Constraints

- **诚实判语不打折**：漏斗每阶段必须显示 verdict 三态（found/not_found/degraded），degraded 显式标红/黄，不得只显示 hits。
- **旧通道不拆**：`/api/search/trace` + `/v12/search/trace` 保持原样（今天页依赖）；新面板走新代理。
- **不引入构建链**：前端仍是纯静态 HTML + Alpine，不装 node、不打包。
- **零新增依赖**。
- TDD：新增代理路由先写失败测试（断言路由存在 + 转发正确），再实现。

---

## File Structure

- **Modify `dashboard/backend/main.py`** — 新增 `POST /api/query/trace`（转发 `/v8/query?trace=true`）；`/api/system` 增补 `lane_profile`/`layer_counts` 字段（PULSE 用）。
- **Modify `dashboard/frontend/index.html`** — 漏斗面板 `runTrace()` 切到 `/api/query/trace`，渲染 6 阶段 + verdict 徽章；「系统」tab 增 PULSE 卡片。
- **Modify/create `tests/test_p42_dashboard_funnel.py`** — 新测试文件，覆盖新代理路由 + 前端关键标记存在性（沿用 p47 的 `importlib` 模式）。

Non-goals: MAP 星图、SETTINGS 只读配置、VAULT 检索面板重构、后端托管 /ui 迁移、`mimir_v8/*` 任何改动。

---

### Task 1: 后端窄代理 `POST /api/query/trace`

**Files:**
- Modify: `dashboard/backend/main.py`（在既有 `api_search_trace`（1161）之后追加）
- Test: `tests/test_p42_dashboard_funnel.py`（新建）

**Interfaces:**
- Consumes: `MIMIR_API`、`_get_admin_token()`（均已在 main.py）
- Produces: `POST /api/query/trace`，body `{"text": str, "limit": int=10}`，转发到 `{MIMIR_API}/v8/query?trace=true`，原样返回 Mímir 响应（含 `recall_trace`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_p42_dashboard_funnel.py
import importlib.util, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "dashboard" / "backend" / "main.py"
INDEX = ROOT / "dashboard" / "frontend" / "index.html"


def _load():
    spec = importlib.util.spec_from_file_location("dash_main_p42", str(MAIN))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_p42_dashboard_funnel.py -v`
Expected: FAIL（路由不存在 / html 无 `/api/query/trace`）

- [ ] **Step 3: 实现代理**（在 `main.py` `api_search_trace` 之后加）

```python
@app.post("/api/query/trace")
async def api_query_trace(body: dict):
    """③-1 RECALL 漏斗（代理 POST /v8/query?trace=true）。

    与 /api/search/trace 并列而非替换：旧通道供今天页「问我的助手」，
    新通道供「检索洞察」漏斗面板，消费六阶段 + 每步 verdict。
    """
    text = (body or {}).get("text", "")
    if not text:
        return {"status": "error", "error": "text required"}
    limit = int((body or {}).get("limit", 10))
    token = _get_admin_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"text": text, "limit": limit, "candidate_limit": 50}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{MIMIR_API}/v8/query?trace=true", headers=headers, json=payload,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
```

- [ ] **Step 4: 前端 `runTrace()` 切通道**

在 `index.html`：`runTrace()` 里 `/api/search/trace` → `/api/query/trace`（去掉 `dedup_threshold` 参数——新通道无此参；保留 `limit`）。**只改 `runTrace`（约 1922 行）那一处**，别动 1743 行的 `askAssistant`。

- [ ] **Step 5: 渲染 verdict**（漏斗阶段卡片）

在阶段卡片（约 1114-1135 行）加 verdict 徽章：`x-text="s.verdict"` 并按值着色（`found`→绿、`not_found`→灰、`degraded`→红）。阶段名从 `s.stage` 读（新结构），`s.hit`/`s.elapsed_ms` 同名兼容。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_p42_dashboard_funnel.py tests/test_p47_dashboard_routes.py -v`
Expected: PASS（含 p47 无回归）

- [ ] **Step 7: 提交**

```bash
git add dashboard/backend/main.py dashboard/frontend/index.html tests/test_p42_dashboard_funnel.py
git commit -m "feat(1.3.0-③-2): 漏斗面板切 /v8/query?trace=true — 六阶段+verdict 渲染，旧通道保留"
```

---

### Task 2: PULSE 健康面板（系统 tab 升级）

**Files:**
- Modify: `dashboard/backend/main.py`（`/api/system` 增补）
- Modify: `dashboard/frontend/index.html`（系统 tab 增 PULSE 卡片）
- Test: `tests/test_p42_dashboard_funnel.py`（追加）

**Interfaces:**
- Consumes: `/api/system` 既有返回 + 新增 `layer_counts`
- Produces: 系统 tab 显示版本/代号/三层存量；`/api/system` 增 `layer_counts: {"l3": n, "l2": n, "l1": n}`

- [ ] **Step 1: 写失败测试**

```python
class TestPulsePanel(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_system_has_layer_counts_sql(self):
        src = MAIN.read_text(encoding="utf-8")
        self.assertIn("layer_counts", src, "/api/system 必须提供三层存量")

    def test_frontend_has_pulse_card(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("PULSE", html, "系统 tab 必须有 PULSE 面板")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_p42_dashboard_funnel.py -v -k pulse`
Expected: FAIL

- [ ] **Step 3: 实现** — `/api/system` 返回里加：

```python
    layer1 = ("event", "project_config", "ephemeral", "learning", "reference")
    layer_rows = _db_query(
        "SELECT CASE WHEN fact_type IN ('iron_rule','user_pref','skill') THEN 'l3' "
        "WHEN fact_type = 'pattern' THEN 'l2' "
        f"WHEN fact_type IN {layer1!r} THEN 'l1' ELSE 'other' END AS layer, "
        "COUNT(*) AS cnt FROM facts WHERE status='active' GROUP BY layer"
    )
    layer_counts = {r["layer"]: r["cnt"] for r in layer_rows}
```

返回 dict 加 `"layer_counts": layer_counts,`。

- [ ] **Step 4: 前端 PULSE 卡片** — 系统 tab「服务信息」卡片上方加一张 PULSE 卡：版本 + 代号（硬编码 `1.x` 系列名或读 `/health`）+ 三层存量条（l3/l2/l1 + other）。

- [ ] **Step 5: 跑测试** — `python -m pytest tests/test_p42_dashboard_funnel.py tests/test_p47_dashboard_routes.py -v` → PASS

- [ ] **Step 6: 提交**

```bash
git add dashboard/backend/main.py dashboard/frontend/index.html tests/test_p42_dashboard_funnel.py
git commit -m "feat(1.3.0-③-2): 系统 tab 升级 PULSE — 三层存量 + 模块探针可见"
```

---

## Self-Review

**Spec coverage（§三 ③-2）：**
- "PULSE 健康面板升级（版本/代号/核心模块探针/存量+容量水位）" → Task 2（版本已有；代号 + 三层存量新增；探针已有 projectors/timers）。
- "RECALL 漏斗面板（③-1 可视化）" → Task 1（切新代理 + verdict 渲染）。
- "MAP/SETTINGS/VAULT 本次不做" → 非目标已列。
- 验收"与现有 VAULT 检索面板数据一致" → 两处最终都打 `MIMIR_API`，同一 query 同一内核；测试覆盖路由层，一致性由共享内核保证。

**Placeholder scan:** 无 TBD。每个 Step 有确切代码/命令/期望。前端行号给了区间（面板结构已读），实现者读文件定位。

**Type consistency:** `recall_trace.stages[].{stage,verdict,hit,elapsed_ms,detail}`（③-1 已上线实测确认）与 Task 1 前端渲染字段一致；`layer_counts` 键 `l3/l2/l1/other` 后端定义与前端消费一致。

**风险：** 前端是单文件 2239 行 Alpine——改动集中在 runTrace + 阶段卡片 + 系统 tab 三处，插入点已给行号区间。若 Alpine 绑定出错，p47 前端测试（fetchJSON 三路返回）会兜住一部分。
