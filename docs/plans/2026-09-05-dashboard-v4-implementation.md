# Dashboard v4 客户视图重构 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 13-tab 开发者控制台重构为 3-tab 客户视图（今天/记忆库/设置）+ 开发者模式开关，旧功能零丢失。

**Architecture:** 后端 FastAPI 加 3 个聚合端点（today/library/health-light，单请求单屏）；前端 Alpine 单文件双套 nav 渲染（客户 3 tab 默认，开发者模式恢复旧 13 tab）；审批动作复用现有端点。

**Tech Stack:** FastAPI + httpx（后端已装）；Alpine.js 3.x 单文件前端（无构建链）；pytest（设备端 v14 venv 跑）。

**Spec:** `docs/plans/2026-09-05-dashboard-v4-customer-redesign-design.md`（commit e5dd2df）

## Global Constraints

- 不引前端框架/构建链——Alpine 单文件结构保留（spec §四）
- 不动旧 13 tab 的任何端点与面板代码——只加聚合层（spec §3.1）
- 客户语言映射三档置信：≥0.8 很确定 / 0.5~0.8 还行 / <0.5 待观察（spec §2.5）
- 空态文案明确非空白（spec §3.4）
- 部署树改动须留 `.bak-v4-<date>` 备印；repo 与部署树同步 commit（spec §3.5）
- auth 中间件不动——新 `/api/dashboard/*` 端点自动落进现有 `/api/` 鉴权面
- 测试跑在设备端 v14 venv：`<设备用户主目录>/.hermes/mimir/venvs/v14.0.0-20260903/bin/python3 -m pytest`
- CRLF 域墙：新文件 LF 入库；改 `dashboard/frontend/index.html` 前后必 `git ls-files --eol` 查（repo 该文件现状 LF）
- 置信度小数、fact_id、decay_tier 等技术词不得出现在客户视图卡片正面（spec §2.5）

## 文件结构

| 文件 | 职责 |
|---|---|
| `dashboard/main.py`（改） | 尾部追加 3 聚合端点（~150 行）：`/api/dashboard/today`、`/api/dashboard/library`、`/api/dashboard/health-light`；版本号 3.0.1→4.0.0 |
| `dashboard/frontend/index.html`（改） | ①navItems 双套（客户 3+开发 13）②顶部健康灯+搜索框 ③今天 tab 时间线（新 `<div class="tab-content" :class="{'active':activeTab==='today'}">`）④记忆库 tab（`library`）⑤设置 tab（`settings`，含开发者模式开关+学习源）⑥switchTab 兼容客户 tab id |
| `tests/test_p41_dashboard_aggregates.py`（新） | 3 聚合端点 TDD：正常/partial 降级/空态三态×3 端点 |

---

### Task 1: 后端 `/api/dashboard/today` 聚合端点（TDD）

**Files:**
- Modify: `dashboard/main.py`（尾部追加，`/api/system` 端点后）
- Create: `tests/test_p41_dashboard_aggregates.py`

**Interfaces:**
- Consumes: 既有 `_mimir_get(path)`（main.py:207）、`_db_query(query, params)`（main.py:237）、`@_cached(key, ttl)` 装饰器（main.py:185）
- Produces: `GET /api/dashboard/today` → JSON：
  ```json
  {
    "date": "2026-09-05",
    "groups": [
      {"label": "今天", "kind": "today",
       "learned": [{"summary": "...", "domain": "quantstar", "confidence": 0.9,
                    "confidence_label": "很确定", "source_name": "导师对话",
                    "created_at": "...", "fact_id": "...", "review_required": false}],
       "pending": [<同 learned 形状 + "candidate_id">],
       "decayed": [{"summary": "...", "last_touched": "..."}]}
    ],
    "degraded": false,
    "error_sources": []
  }
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_p41_dashboard_aggregates.py
"""P41 Dashboard v4 聚合端点测试 — spec: docs/plans/2026-09-05-dashboard-v4-customer-redesign-design.md §3.2"""
import importlib.util, sys
from pathlib import Path

def _load_main(monkeypatch):
    """加载 dashboard/main.py 并拦截其 _mimir_get/_db_query（不真连生产 API）。"""
    spec = importlib.util.spec_from_file_location(
        "dash_main", str(Path(__file__).resolve().parents[1] / "dashboard" / "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class TestTodayEndpoint:
    def test_normal_shape(self, monkeypatch):
        mod = _load_main(monkeypatch)
        # 伪数据：今天学到 1 条 + 待审 1 条 + 淡忘 1 条
        async def fake_get(path):
            if path == "/v8/learning/candidates":
                return {"candidates": [{
                    "candidate_id": "c1", "content": "用户偏好夜间部署",
                    "summary": "用户偏好夜间部署", "proposed_domain": "ops",
                    "confidence_score": 0.4, "created_at": "2026-09-05T10:00:00+08:00",
                    "status": "review_required"}]}
            return None
        monkeypatch.setattr(mod, "_mimir_get", fake_get)
        rows = [
            {"domain": "quantstar", "summary": "铁律：qs 与 mimir 分离", "confidence": 1.0,
             "source_name": "导师对话", "created_at": "2026-09-05T09:00:00+08:00",
             "fact_id": "f1", "review_required": 0},
            {"domain": "ops", "summary": "旧备份策略", "confidence": 0.3,
             "source_name": "cron", "last_touched": "2026-08-20T00:00:00+08:00"},
        ]
        def fake_q(query, params=(), database=None):
            if "FROM facts" in query and "created_at" in query:
                return [rows[0]]
            if "decay" in query.lower() or "last_touched" in query:
                return [rows[1]]
            return []
        monkeypatch.setattr(mod, "_db_query", fake_q)
        import asyncio
        result = asyncio.run(mod.api_dashboard_today())
        g = result["groups"][0]
        assert g["kind"] == "today"
        assert g["learned"][0]["confidence_label"] == "很确定"       # 1.0 → ≥0.8 档
        assert g["pending"][0]["candidate_id"] == "c1"
        assert g["pending"][0]["confidence_label"] == "待观察"      # 0.4 → <0.5 档
        assert result["degraded"] is False

    def test_partial_degraded(self, monkeypatch):
        mod = _load_main(monkeypatch)
        async def dead_get(path): return None          # Mímir API 全断
        monkeypatch.setattr(mod, "_mimir_get", dead_get)
        monkeypatch.setattr(mod, "_db_query", lambda q, p=(), database=None: [])
        import asyncio
        result = asyncio.run(mod.api_dashboard_today())
        assert result["degraded"] is True
        assert result["error_sources"]                  # 至少标一个上游源

    def test_empty_state_not_blank(self, monkeypatch):
        mod = _load_main(monkeypatch)
        async def ok_get(path): return {"candidates": []}
        monkeypatch.setattr(mod, "_mimir_get", ok_get)
        monkeypatch.setattr(mod, "_db_query", lambda q, p=(), database=None: [])
        import asyncio
        result = asyncio.run(mod.api_dashboard_today())
        g = result["groups"][0]
        assert g["learned"] == [] and g["pending"] == [] and g["decayed"] == []
        assert result["degraded"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd <设备用户主目录>/mimir-open-source && .hermes/mimir/venvs/v14.0.0-20260903/bin/python3 -m pytest tests/test_p41_dashboard_aggregates.py -v`（设备端）
Expected: FAIL — `AttributeError: module 'dash_main' has no attribute 'api_dashboard_today'`

- [ ] **Step 3: 最小实现（main.py 尾部追加）**

```python
# ── v4: customer-view aggregates ─────────────────────────────────────────
V4_CONFIDENCE_LABELS = ((0.8, "很确定"), (0.5, "还行"), (0.0, "待观察"))

def _v4_conf_label(c: float) -> str:
    for floor, label in V4_CONFIDENCE_LABELS:
        if (c or 0) >= floor:
            return label
    return "待观察"

@app.get("/api/dashboard/today")
@_cached("dash_today", ttl=60)
async def api_dashboard_today():
    """客户视图·今天 — 学习事件+待审+淡忘，单请求。spec §2.1"""
    error_sources: list[str] = []
    learned: list[dict] = []
    pending: list[dict] = []
    decayed: list[dict] = []

    cand = await _mimir_get("/v8/learning/candidates?status=review_required&limit=20")
    if cand is None:
        error_sources.append("learning_candidates")
    else:
        for c in (cand.get("candidates") or [])[:20]:
            pending.append({
                "candidate_id": c.get("candidate_id"),
                "summary": c.get("summary") or (c.get("content") or "")[:80],
                "domain": c.get("proposed_domain", ""),
                "confidence": c.get("confidence_score"),
                "confidence_label": _v4_conf_label(c.get("confidence_score") or 0),
                "source_name": c.get("source_name", "学习引擎"),
                "created_at": c.get("created_at"),
            })

    try:
        rows = _db_query(
            "SELECT domain, summary, confidence, source_name, created_at, fact_id, review_required "
            "FROM facts WHERE status='active' AND date(created_at)=date('now','localtime') "
            "ORDER BY created_at DESC LIMIT 30")
        for r in rows:
            learned.append({
                "summary": r["summary"], "domain": r["domain"],
                "confidence": r["confidence"],
                "confidence_label": _v4_conf_label(r["confidence"] or 0),
                "source_name": r["source_name"] or "记忆管家",
                "created_at": r["created_at"], "fact_id": r["fact_id"],
                "review_required": bool(r["review_required"]),
            })
    except Exception:
        error_sources.append("facts_today")

    try:
        rows = _db_query(
            "SELECT summary, last_touched FROM facts "
            "WHERE status='active' AND decay_tier IN ('L4_temporary','L5_archive') "
            "AND date(last_touched)>=date('now','-3 days','localtime') LIMIT 10")
        decayed = [{"summary": r["summary"], "last_touched": r["last_touched"]} for r in rows]
    except Exception:
        error_sources.append("decayed_recent")

    return {
        "date": __import__("datetime").date.today().isoformat(),
        "groups": [{"label": "今天", "kind": "today",
                    "learned": learned, "pending": pending, "decayed": decayed}],
        "degraded": bool(error_sources),
        "error_sources": error_sources,
    }
```

- [ ] **Step 4: 跑测试确认过**

Run: 同 Step 2。Expected: TestTodayEndpoint 3/3 PASS

- [ ] **Step 5: Commit**

```bash
git add dashboard/main.py tests/test_p41_dashboard_aggregates.py
git commit -m "feat(dashboard): /api/dashboard/today aggregate — customer today-timeline (TDD)"
```

---

### Task 2: 后端 `/api/dashboard/library` + `/api/dashboard/health-light`（TDD）

**Files:**
- Modify: `dashboard/main.py`（Task 1 块内继续追加）
- Modify: `tests/test_p41_dashboard_aggregates.py`（追加两测试族）

**Interfaces:**
- Consumes: Task 1 的 `_v4_conf_label`；既有 `_mimir_get`、`_db_query`、`_compute_alerts`（main.py:259）
- Produces:
  - `GET /api/dashboard/library?offset=0&limit=50&filter=all|disputed|pending` → `{"cards": [<今日 learned 同形状>], "total": 123, "pending_count": 2, "disputed_count": 0, "degraded": bool}`
  - `GET /api/dashboard/health-light` → `{"light": "green|yellow|red", "reasons": [...], "checked_at": "..."}`

- [ ] **Step 1: 写失败测试（追加到同文件）**

```python
class TestLibraryEndpoint:
    def test_cards_and_filters(self, monkeypatch):
        mod = _load_main(monkeypatch)
        monkeypatch.setattr(mod, "_mimir_get", lambda path: _async_ret({"candidates": []}))
        def fake_q(query, params=(), database=None):
            if "COUNT" in query.upper():
                return [{"cnt": 7}]
            if "disputed" in query.lower():
                return []
            return [{"domain": "ops", "summary": "卡片一", "confidence": 0.62,
                      "source_name": "导师对话", "created_at": "2026-09-05T08:00:00+08:00",
                      "fact_id": "f9", "review_required": 0, "status": "active"}]
        monkeypatch.setattr(mod, "_db_query", fake_q)
        import asyncio
        r = asyncio.run(mod.api_dashboard_library())
        assert r["cards"][0]["confidence_label"] == "还行"     # 0.62 → 0.5~0.8 档
        assert r["total"] >= 1

    def test_filter_disputed(self, monkeypatch):
        mod = _load_main(monkeypatch)
        monkeypatch.setattr(mod, "_mimir_get", lambda p: _async_ret({"candidates": []}))
        monkeypatch.setattr(mod, "_db_query",
            lambda q, p=(), database=None: ([{"cnt": 1}] if "COUNT" in q.upper() else
                [{"domain": "x", "summary": "分歧卡", "confidence": 0.5,
                  "source_name": "双源", "created_at": "2026-09-04T08:00:00+08:00",
                  "fact_id": "f2", "review_required": 0}]))
        import asyncio
        r = asyncio.run(mod.api_dashboard_library(filter="disputed"))
        assert r["cards"][0]["fact_id"] == "f2"

def _async_ret(v):
    async def f(path=None): return v
    return f

class TestHealthLightEndpoint:
    def test_green(self, monkeypatch):
        mod = _load_main(monkeypatch)
        async def fake_get(path):
            if path == "/ready":
                return {"status": "ready", "dead_letters": 0, "pending": 0,
                        "projectors": [{"projector_name": "fts", "status": "idle",
                                         "last_error_code": None, "pending": 0, "dead_letter": 0}]}
            if path == "/v8/learning/status":
                return {"candidates": {"review_required": 2}}
            return None
        monkeypatch.setattr(mod, "_mimir_get", fake_get)
        import asyncio
        r = asyncio.run(mod.api_dashboard_health_light())
        assert r["light"] == "green"

    def test_yellow_pending_backlog(self, monkeypatch):
        mod = _load_main(monkeypatch)
        async def fake_get(path):
            if path == "/ready":
                return {"status": "ready", "dead_letters": 0, "pending": 0,
                        "projectors": [{"projector_name": "fts", "status": "idle",
                                         "last_error_code": None, "pending": 0, "dead_letter": 0}]}
            if path == "/v8/learning/status":
                return {"candidates": {"review_required": 31}}   # >30 待审 → 黄
            return None
        monkeypatch.setattr(mod, "_mimir_get", fake_get)
        import asyncio
        r = asyncio.run(mod.api_dashboard_health_light())
        assert r["light"] == "yellow"
        assert any("待审" in x for x in r["reasons"])

    def test_red_api_down(self, monkeypatch):
        mod = _load_main(monkeypatch)
        async def dead(path): return None
        monkeypatch.setattr(mod, "_mimir_get", dead)
        import asyncio
        r = asyncio.run(mod.api_dashboard_health_light())
        assert r["light"] == "red"
```

- [ ] **Step 2: 跑测试确认失败**

Run: 同 Task 1 Step 2。Expected: FAIL — `api_dashboard_library`/`api_dashboard_health_light` 不存在

- [ ] **Step 3: 最小实现（追加）**

```python
@app.get("/api/dashboard/library")
@_cached("dash_library", ttl=60)
async def api_dashboard_library(offset: int = 0, limit: int = 50,
                                filter: str = "all"):
    """客户视图·记忆库 — 全量卡片流。spec §2.1"""
    error_sources: list[str] = []
    where = "status='active'"
    if filter == "disputed":
        where += " AND disputed=1"
    try:
        total_rows = _db_query(
            f"SELECT COUNT(*) AS cnt FROM facts WHERE {where}")
        total = total_rows[0]["cnt"] if total_rows else 0
        cards = []
        for r in _db_query(
            f"SELECT domain, summary, confidence, source_name, created_at, fact_id, review_required "
            f"FROM facts WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)):
            cards.append({
                "summary": r["summary"], "domain": r["domain"],
                "confidence": r["confidence"],
                "confidence_label": _v4_conf_label(r["confidence"] or 0),
                "source_name": r["source_name"] or "记忆管家",
                "created_at": r["created_at"], "fact_id": r["fact_id"],
                "review_required": bool(r["review_required"]),
            })
    except Exception:
        total, cards, error_sources = 0, [], ["library_query"]

    pending_count = len((await _mimir_get(
        "/v8/learning/candidates?status=review_required&limit=1") or {}).get("candidates") or [])
    return {"cards": cards, "total": total,
            "pending_count": pending_count if isinstance(pending_count, int) else 0,
            "degraded": bool(error_sources), "error_sources": error_sources}

@app.get("/api/dashboard/health-light")
@_cached("dash_light", ttl=60)
async def api_dashboard_health_light():
    """客户视图·健康灯。spec §2.4 绿/黄/红"""
    ready = await _mimir_get("/ready")
    learning = await _mimir_get("/v8/learning/status")
    if not ready:
        return {"light": "red", "reasons": ["Mímir API 不可达"], "checked_at": _now_iso()}
    reasons: list[str] = []
    if ready.get("status") != "ready":
        reasons.append("API 未就绪")
    if ready.get("dead_letters", 0) > 0:
        reasons.append(f"死信 {ready['dead_letters']} 条")
    bad_proj = [p["projector_name"] for p in ready.get("projectors", [])
                if p.get("last_error_code") or p.get("status") not in ("idle", "running")]
    if bad_proj:
        reasons.append(f"投影器异常: {', '.join(bad_proj)}")
    pending = (learning or {}).get("candidates", {}).get("review_required", 0) or 0
    if pending > 30:
        reasons.append(f"待审堆积 {pending} 条")
    light = "red" if ready.get("status") != "ready" else ("yellow" if reasons else "green")
    return {"light": light, "reasons": reasons,
            "pending_count": pending, "checked_at": _now_iso()}

def _now_iso():
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")
```

- [ ] **Step 4: 跑测试确认过**

Run: 同上。Expected: 新 3 族+旧 3 族全 PASS（6/6）

- [ ] **Step 5: Commit**

```bash
git add dashboard/main.py tests/test_p41_dashboard_aggregates.py
git commit -m "feat(dashboard): library + health-light aggregates (TDD)"
```

---

### Task 3: 前端 — 双套 nav + 健康灯 + 搜索框 + 今天 tab

**Files:**
- Modify: `dashboard/frontend/index.html`

**Interfaces:**
- Consumes: Task 1/2 三端点；既有 `fetchJSON(url)`、`switchTab(id)`、`navItems` 数组（index.html:1278）
- Produces: Alpine state `customerView: {today: {}, library: {}, light: {}, search: '', searchResults: []}`；`devMode: false`（localStorage 持久 `devMode`）；nav 渲染表达式 `devMode ? navItems : customerNav`

- [ ] **Step 1: navItems 双套 + 客户 state（改 `navItems:` 定义处，1278 行附近）**

在 `navItems: [...]` 闭合 `],` 后追加：

```javascript
    // v4 客户视图 nav（spec §2.1）——devMode=false 时显示这套
    customerNav: [
      {id:'today',label:'今天',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>'},
      {id:'library',label:'记忆库',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>'},
      {id:'settings',label:'设置',icon:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.38.63 1 1.04 1.51 1h.09a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'},
    ],
    devMode: (localStorage.getItem('devMode')==='1'),
    customerView: {today:{}, light:{}, library:{}, libFilter:'all', libOffset:0, search:'', searchResults:[], searchBusy:false, lightOpen:false},
```

sidebar 的 `x-for` 改为两处渲染（nav 渲染表达式）：`<template x-for="item in (devMode ? navItems : customerNav)">`（desktop 侧 1254 行 + mobile nav 141 行两处）。

- [ ] **Step 2: 健康灯 + 搜索框（顶栏插入）**

在现有顶部 header 结构（含 darkMode 切换的容器）内追加：

```html
    <!-- v4 健康灯：绿/黄/红，点开详情浮层 -->
    <div style="display:flex;align-items:center;gap:8px">
      <button @click="customerView.lightOpen=!customerView.lightOpen" title="系统健康"
              style="background:none;border:none;cursor:pointer;display:flex;align-items:center;gap:6px">
        <span :style="`width:12px;height:12px;border-radius:50%;background:${{green:'#22c55e',yellow:'#eab308',red:'#ef4444',off:'#94a3b8'}[customerView.light.light||'off']}`"></span>
        <span x-show="customerView.light.reasons && customerView.light.reasons.length"
              style="font-size:.72rem;color:var(--red)" x-text="(customerView.light.reasons||[])[0]"></span>
      </button>
      <div x-show="customerView.lightOpen" @click.outside="customerView.lightOpen=false"
           style="position:absolute;top:52px;right:16px;z-index:60;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;min-width:240px;box-shadow:0 8px 24px rgba(0,0,0,.12)">
        <div style="font-weight:600;margin-bottom:6px">系统健康</div>
        <template x-for="r in (customerView.light.reasons||[]).length ? customerView.light.reasons : ['一切正常']">
          <div style="font-size:.78rem;color:var(--muted);padding:2px 0" x-text="'· ' + r"></div>
        </template>
      </div>
    </div>
    <!-- v4 问我的助手：常驻搜索 -->
    <input x-model="customerView.search" @keydown.enter="askAssistant()"
           placeholder="问我的助手…"
           style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:8px 14px;font-size:.85rem;width:220px">
```

- [ ] **Step 3: 今天 tab 面板（13 个旧 `tab-content` div 前插入新 div）**

```html
<div class="tab-content" :class="{'active':activeTab==='today'}" x-show="!devMode">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
    <h2 style="font-size:1.15rem">我的记忆管家</h2>
    <span style="font-size:.75rem;color:var(--muted)" x-text="'今天 · ' + (customerView.today.date||'')"></span>
  </div>
  <div class="card" style="margin-bottom:14px">
    <div style="font-weight:600;margin-bottom:10px">📖 今天学到 <span x-text="((customerView.today.groups||[])[0]?.learned||[]).length"></span> 条</div>
    <template x-for="c in ((customerView.today.groups||[])[0]?.learned||[])">
      <div class="card card-hover" style="padding:12px;margin-bottom:8px;border:1px solid var(--border)">
        <div style="font-size:.9rem" x-text="c.summary"></div>
        <div style="display:flex;gap:10px;font-size:.72rem;color:var(--muted);margin-top:6px">
          <span x-text="'从哪学的：' + c.source_name"></span>
          <span x-text="'确定程度：' + c.confidence_label"></span>
        </div>
      </div>
    </template>
    <div class="empty" x-show="!((customerView.today.groups||[])[0]?.learned||[]).length">
      今天还没有新记忆，去和你的助手聊聊吧
    </div>
  </div>
  <div class="card" style="margin-bottom:14px" x-show="(((customerView.today.groups||[])[0]?.pending||[]).length)">
    <div style="font-weight:600;margin-bottom:10px">💬 <span x-text="((customerView.today.groups||[])[0]?.pending||[]).length"></span> 条等你确认</div>
    <template x-for="c in ((customerView.today.groups||[])[0]?.pending||[])">
      <div class="card" style="padding:12px;margin-bottom:8px;border:1px solid var(--border)">
        <div style="font-size:.9rem" x-text="c.summary"></div>
        <div style="display:flex;gap:10px;font-size:.72rem;color:var(--muted);margin-top:6px">
          <span x-text="'从哪学的：' + c.source_name"></span>
          <span x-text="'确定程度：' + c.confidence_label"></span>
        </div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn btn-sm" @click="reviewPending(c.candidate_id,'approve')">✓ 是这样</button>
          <button class="btn btn-outline btn-sm" @click="reviewPending(c.candidate_id,'reject')">✗ 纠正它</button>
        </div>
      </div>
    </template>
  </div>
  <div class="card" x-show="(((customerView.today.groups||[])[0]?.decayed||[]).length)">
    <div style="font-weight:600;margin-bottom:10px">🗑 已淡忘 <span x-text="((customerView.today.groups||[])[0]?.decayed||[]).length"></span> 条</div>
    <template x-for="c in ((customerView.today.groups||[])[0]?.decayed||[])">
      <div style="font-size:.85rem;color:var(--muted);padding:6px 0" x-text="'“' + c.summary + '” 已淡忘'"></div>
    </template>
  </div>
</div>
```

Alpine 方法（`fetchJSON` 定义后追加）：

```javascript
    async loadToday() {
      this.customerView.today = await this.fetchJSON('/api/dashboard/today');
    },
    async loadLight() {
      this.customerView.light = await this.fetchJSON('/api/dashboard/health-light');
    },
    async reviewPending(cid, action) {
      await this.fetchJSON(`/api/candidates/${cid}/review?action=${action}`, {method:'POST'});
      await this.loadToday(); this.refresh();
    },
    async askAssistant() {
      const q = this.customerView.search.trim();
      if (!q) return;
      this.customerView.searchBusy = true;
      const r = await this.fetchJSON('/api/search/trace', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({query: q})});
      this.customerView.searchResults = r.results || [];
      this.customerView.searchBusy = false;
    },
```

`init()`/`refresh()` 里追加 `this.loadToday(); this.loadLight();`；`switchTab` 开头补 `if (id==='library') this.loadLibrary();`（Task 4 接线）。

- [ ] **Step 4: 本地校验**

Run: `node -e "const h=require('fs').readFileSync('dashboard/frontend/index.html','utf8'); const m=h.match(/<script>([\s\S]*)<\/script>/); new Function(m[1]); console.log('JS OK')"`（设备端；语法级冒烟）
Expected: `JS OK`

- [ ] **Step 5: Commit**

```bash
git add dashboard/frontend/index.html
git commit -m "feat(dashboard): customer nav + health light + today timeline (v4)"
```

---

### Task 4: 前端 — 记忆库 tab + 设置 tab（含开发者模式开关）

**Files:**
- Modify: `dashboard/frontend/index.html`

**Interfaces:**
- Consumes: Task 2 `/api/dashboard/library`；既有 `/api/candidates`、来源管理（`newSource`）
- Produces: `loadLibrary()` 方法；设置面板含 `devMode` 开关 + 学习源卡片 + 清理策略说明

- [ ] **Step 1: 记忆库 tab 面板（today div 后插入）**

```html
<div class="tab-content" :class="{'active':activeTab==='library'}" x-show="!devMode">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
    <h2 style="font-size:1.15rem">记忆库</h2>
    <div style="display:flex;gap:8px">
      <button class="btn btn-sm" :class="{'btn-outline':customerView.libFilter!=='all'}" @click="customerView.libFilter='all';customerView.libOffset=0;loadLibrary()">全部</button>
      <button class="btn btn-sm" :class="{'btn-outline':customerView.libFilter!=='disputed'}" @click="customerView.libFilter='disputed';customerView.libOffset=0;loadLibrary()">有分歧的</button>
      <button class="btn btn-sm" :class="{'btn-outline':customerView.libFilter!=='pending'}" @click="customerView.libFilter='pending';customerView.libOffset=0;loadLibrary()">待我确认 <span x-show="customerView.library.pending_count" x-text="customerView.library.pending_count"></span></button>
    </div>
  </div>
  <template x-for="c in (customerView.library.cards||[])">
    <div class="card card-hover" style="padding:12px;margin-bottom:8px;border:1px solid var(--border)">
      <div style="font-size:.9rem" x-text="c.summary"></div>
      <div style="display:flex;gap:10px;font-size:.72rem;color:var(--muted);margin-top:6px">
        <span x-text="'从哪学的：' + c.source_name"></span>
        <span x-text="'确定程度：' + c.confidence_label"></span>
        <span x-text="formatTime(c.created_at)"></span>
      </div>
    </div>
  </template>
  <div class="empty" x-show="!(customerView.library.cards||[]).length">记忆库还是空的</div>
  <div style="display:flex;gap:8px;margin-top:12px">
    <button class="btn btn-outline btn-sm" x-show="customerView.libOffset>0" @click="customerView.libOffset=Math.max(0,customerView.libOffset-50);loadLibrary()">← 上一页</button>
    <button class="btn btn-outline btn-sm" @click="customerView.libOffset+=50;loadLibrary()">下一页 →</button>
  </div>
</div>
```

`loadLibrary()`：

```javascript
    async loadLibrary() {
      this.customerView.library = await this.fetchJSON(
        `/api/dashboard/library?offset=${this.customerView.libOffset}&limit=50&filter=${this.customerView.libFilter}`);
    },
```

（后端 `filter=pending` 分支：`where += " AND review_required=1"`——Task 2 实现里补此分支）

- [ ] **Step 2: 设置 tab（settings div）**

```html
<div class="tab-content" :class="{'active':activeTab==='settings'}" x-show="!devMode">
  <h2 style="font-size:1.15rem;margin-bottom:14px">设置</h2>
  <div class="card" style="margin-bottom:14px">
    <div style="font-weight:600;margin-bottom:8px">我的助手在哪学习</div>
    <template x-for="s in (sourcesData.sources||[])">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:.85rem" x-text="s.name || s.url || s.source_id"></span>
        <span class="tag" x-text="s.kind || s.type || 'source'"></span>
      </div>
    </template>
    <div class="empty" x-show="!(sourcesData.sources||[]).length">还没有接入学习源</div>
    <!-- 复用现有 newSource 表单：直接沿用旧来源 tab 的添加表单结构（粘贴同款 input+button） -->
  </div>
  <div class="card" style="margin-bottom:14px">
    <div style="font-weight:600;margin-bottom:8px">记忆怎么淡化</div>
    <div style="font-size:.8rem;color:var(--muted)">长期没被用到的记忆会慢慢淡化，重要的记忆通过你的确认保持新鲜。你确认过的记忆不会被淡化。</div>
  </div>
  <div class="card">
    <div style="font-weight:600;margin-bottom:8px">开发者模式</div>
    <div style="font-size:.8rem;color:var(--muted);margin-bottom:10px">打开后显示完整运维面板（13 个高级标签页）。日常不需要。</div>
    <button class="btn btn-sm" :class="devMode ? '' : 'btn-outline'" @click="devMode=!devMode; localStorage.setItem('devMode', devMode?'1':'0'); activeTab='overview'">
      <span x-text="devMode ? '关闭开发者模式' : '打开开发者模式'"></span>
    </button>
  </div>
</div>
```

设置 tab 打开时拉源：`switchTab` 里补 `if (id==='settings') this.loadSources();`（沿用现有 `loadSources`）。

- [ ] **Step 3: JS 语法冒烟 + commit**

Run: 同 Task 3 Step 4。Expected: `JS OK`

```bash
git add dashboard/frontend/index.html
git commit -m "feat(dashboard): library + settings tabs with dev-mode toggle (v4)"
```

---

### Task 5: 部署 + 验收 + repo 收尾

**Files:**
- Modify: 部署树 `<设备用户主目录>/mimir-dashboard/backend/main.py` + `<设备用户主目录>/mimir-dashboard/frontend/index.html`（scp 推送）
- Modify: `dashboard/README.md`（v4 客户视图说明）
- Modify: `CHANGELOG.md`（4.0.0 段）

**Interfaces:**
- Consumes: Task 1-4 全部产出
- Produces: live 8800 跑 v4；版本号 4.0.0

- [ ] **Step 1: 版本号 bump**

`dashboard/main.py:50` `version="3.0.1"` → `version="4.0.0"`

- [ ] **Step 2: 部署（备印+推送+重启）**

```bash
cp <设备用户主目录>/mimir-dashboard/backend/main.py <设备用户主目录>/mimir-dashboard/backend/main.py.bak-v4-20260905
cp <设备用户主目录>/mimir-dashboard/frontend/index.html <设备用户主目录>/mimir-dashboard/frontend/index.html.bak-v4-20260905
scp repo 件 → 部署树（backend/main.py + frontend/index.html）
sudo systemctl restart mimir-dashboard.service
```

- [ ] **Step 3: live 验收（客户语言标准，spec §3.6）**

```bash
curl -fsS http://127.0.0.1:8800/api/dashboard/health-light   # 期望 {"light":"green",...}
curl -fsS http://127.0.0.1:8800/api/dashboard/today           # 期望 groups+p,date 今天
# 浏览器走查：登录后默认 3 tab；开 devMode 恢复 13 tab；审批按钮真审一条；搜索框真查一次
```

- [ ] **Step 4: README + CHANGELOG + commit + 四远端**

```bash
git add dashboard/main.py dashboard/README.md CHANGELOG.md dashboard/frontend/index.html
git commit -m "feat(dashboard): v4.0.0 customer view — 3-tab memory butler (ship)"
# 四远端 push（origin github + gitee，从设备 repo 推，遵循既有发布流程）
```

---

## Self-Review 结论

- **Spec 覆盖**：六问裁定→Task 3（时间线+审批内嵌+搜索+灯）；§2.1 三 tab→Task 3/4；§2.3 归属表→Task 3/4（意见→library 筛选；来源→settings）；§2.4 灯→Task 2/3；§2.5 卡片映射+置信三档→Task 1/3；§3.4 空态+降级→Task 1/2/3；开发者模式→Task 4。无缺口。
- **占位符**：无 TBD/TODO；来源添加表单明确「粘贴旧 tab 同款结构」非悬空引用。
- **类型一致**：`confidence_label`（Task 1 定、Task 2/3/4 用）；`loadToday/loadLight/loadLibrary/reviewPending/askAssistant`（Task 3 定、Task 4 引用）已对齐。
