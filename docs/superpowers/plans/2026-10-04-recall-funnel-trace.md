# RECALL 召回漏斗 Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /v8/query?trace=true` returns a per-stage recall funnel trace with honest per-stage verdicts, without changing default (trace-off) behavior or performance.

**Architecture:** New `mimir_v8/recall_trace.py` holds two frozen dataclasses (`RecallStage`, `RecallTrace`). `QueryKernel.search()` instruments the pipeline it already runs (gate → channels → anchor → layer sweep → hydration filter → top-K) and attaches a trace only when `QueryRequest.include_trace` is set. API route passes `?trace=true` through. No changes to `/v12/search/trace`, dashboard, or scoring.

**Tech Stack:** Python 3.11, dataclasses, `time.monotonic`, FastAPI `Query` param, pytest/unittest (repo uses `unittest.TestCase` style in `tests/test_p46_resilience_tier.py` — follow it).

**Spec:** `docs/superpowers/specs/2026-10-04-mimir-aidumei-adoption-design.md` (§三 ③-1)

## Global Constraints

- Per-stage verdict vocabulary is exactly `found | not_found | degraded` (spec §五 discipline 2). No new verdict words.
- `degraded` outranks `not_found`: any fault beats "zero hits" (P0-A / P46 semantics already in `query.py:487-489`).
- Default response unchanged: no `recall_trace` key unless requested (perf; spec §三 ③-1).
- `recall_verdict` top-level computation untouched — single source `_recall_verdict` stays the only place that word is decided.
- TDD: RED test → confirm fail → GREEN → full-file regression before commit.

---

## File Structure

- **Create `mimir_v8/recall_trace.py`** — `RecallStage` + `RecallTrace` frozen dataclasses with `to_dict()`. Zero imports from query/api/store (leaf module, no cycles).
- **Modify `mimir_v8/query.py`** — (a) `QueryRequest` gains `include_trace: bool = False`; (b) `search()` collects stages + attaches `recall_trace`.
- **Modify `mimir_v8/api.py`** — `query_v8` gains `trace: bool = Query(default=False)` query param, passed into `QueryRequest`. `execute_query` signature unchanged (other two call sites at `api.py:1478,1955` unaffected).
- **Create `tests/test_recall_funnel_trace.py`** — fixture clones `ResilienceFixture` pattern from `tests/test_p46_resilience_tier.py:85-100` (fake vector/fts with `.fail` flags, real temp `CanonicalStore`, one iron_rule seed).

Non-goals (do NOT touch): `/v12/search/trace` endpoint, `trace()` method, dashboard proxy, scoring/RRF math, `LaneProfile`.

---

### Task 1: RecallStage / RecallTrace dataclasses

**Files:**
- Create: `mimir_v8/recall_trace.py`
- Test: `tests/test_recall_funnel_trace.py` (Part A only in this task)

**Interfaces:**
- Consumes: nothing.
- Produces: `RecallStage(name, verdict, hits, elapsed_ms, detail) -> dict via .to_dict()`; `RecallTrace(skipped, verdict, stages, lanes, degraded) -> dict via .to_dict()`. Task 2 imports both.

**Verdict contract (locked here, reused in Task 2):**
- `found` = stage produced ≥1 kept item and no fault.
- `not_found` = stage ran clean but kept 0.
- `degraded` = the underlying call faulted or the breaker was open (fault outranks zero-hits).

- [ ] **Step 1: Write the failing test**

```python
def test_stage_to_dict_shape(self):
    from mimir_v8.recall_trace import RecallStage
    s = RecallStage(name="CandidatePool", verdict="found", hits=8,
                    elapsed_ms=1.25, detail={"channels": {"vector": "closed"}})
    d = s.to_dict()
    self.assertEqual(d, {"stage": "CandidatePool", "verdict": "found",
                         "hits": 8, "elapsed_ms": 1.25,
                         "detail": {"channels": {"vector": "closed"}}})

def test_stage_rejects_bad_verdict(self):
    from mimir_v8.recall_trace import RecallStage
    with self.assertRaises(ValueError):
        RecallStage(name="X", verdict="maybe", hits=0, elapsed_ms=0.0)

def test_trace_to_dict_shape(self):
    from mimir_v8.recall_trace import RecallStage, RecallTrace
    t = RecallTrace(skipped=False, verdict="found", degraded=False,
                    lanes={"active": ["vector"]},
                    stages=[RecallStage(name="TopK", verdict="found",
                                        hits=3, elapsed_ms=0.5)])
    d = t.to_dict()
    self.assertEqual(d["verdict"], "found")
    self.assertEqual(len(d["stages"]), 1)
    self.assertEqual(d["stages"][0]["stage"], "TopK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recall_funnel_trace.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mimir_v8.recall_trace'`

- [ ] **Step 3: Write minimal implementation** — create `mimir_v8/recall_trace.py`:

```python
"""RECALL funnel trace value objects (1.3.0 item ③-1).

Leaf module: no imports from query/api/store — it is imported BY them.
Verdict vocabulary is locked to found|not_found|degraded (spec §五-2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

STAGE_VERDICTS = ("found", "not_found", "degraded")


@dataclass(frozen=True)
class RecallStage:
    """One funnel step: what ran, what it kept, how long it took."""
    name: str
    verdict: str
    hits: int
    elapsed_ms: float
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in STAGE_VERDICTS:
            raise ValueError(f"stage verdict must be one of {STAGE_VERDICTS}")

    def to_dict(self) -> dict:
        return {"stage": self.name, "verdict": self.verdict,
                "hits": self.hits, "elapsed_ms": self.elapsed_ms,
                "detail": dict(self.detail)}


@dataclass(frozen=True)
class RecallTrace:
    """Whole-funnel trace attached to a search() response when requested."""
    skipped: bool
    verdict: str
    degraded: bool
    lanes: dict
    stages: list

    def __post_init__(self) -> None:
        if self.verdict not in STAGE_VERDICTS:
            raise ValueError(f"trace verdict must be one of {STAGE_VERDICTS}")

    def to_dict(self) -> dict:
        return {"skipped": self.skipped, "verdict": self.verdict,
                "degraded": self.degraded, "lanes": dict(self.lanes),
                "stages": [s.to_dict() for s in self.stages]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_recall_funnel_trace.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/recall_trace.py tests/test_recall_funnel_trace.py
git commit -m "feat(1.3.0-③-1): RecallStage/RecallTrace value objects — per-stage verdict vocabulary locked to found|not_found|degraded"
```

---

### Task 2: Instrument search() — collect stages, attach trace when requested

**Files:**
- Modify: `mimir_v8/query.py` (`QueryRequest` ~line 96; `search()` lines 322-517)
- Test: `tests/test_recall_funnel_trace.py` (Part B — append new test classes)

**Interfaces:**
- Consumes: `RecallStage`, `RecallTrace` from Task 1; existing locals in `search()`: `channel_states`, `anchor_injected`, `sweep_injected`, `sweep_types`, `filtered`, `ranked`, `results`, `degraded`, `verdict`.
- Produces: `search()` response gains `recall_trace` key **only** when `request.include_trace` is True. Task 3 reads it via API.

**Stage map (mirrors exactly what search() already does — no new pipeline):**

| Stage name | hits source | verdict rule |
|---|---|---|
| `RelevanceGate` | 1 if searched else 0 | `degraded` if gate-error-fallback path taken; `found` if searched; `not_found` if gate skipped |
| `CandidatePool` | `len(ranked)` after channels+anchor+sweep | `degraded` if any channel state is degraded/open; `found` if hits>0 else `not_found`; detail carries per-channel states + hits |
| `AnchorChannel` | `anchor_injected` | `found` if >0 else `not_found` |
| `LayerSweep` | `sweep_injected` | `found` if >0 else `not_found` |
| `HydrationFilter` | `len(results)` pre-limit | `degraded` if top-level degraded else (`found` if >0 else `not_found`); detail carries `filtered` counts (acl/status/missing/layer) |
| `TopK` | `len(top_results)` | `found` if >0 else `not_found` |

Timing: one `_t0 = time.monotonic()` at search start (only when `include_trace`); each stage records cumulative `elapsed_ms` (same convention as existing `trace()` at `query.py:526-534`). `time` is already imported in query.py.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_recall_funnel_trace.py`; fixture = clone of `ResilienceFixture` from `tests/test_p46_resilience_tier.py:85-100` — read that file first, copy the fake vector/fts/store-seed shape):

```python
def test_search_without_trace_has_no_trace_key(self):
    r = self.fx.search()  # include_trace defaults False
    self.assertNotIn("recall_trace", r)

def test_search_with_trace_returns_all_stages(self):
    r = self.fx.search(include_trace=True)
    t = r["recall_trace"]
    names = [s["stage"] for s in t["stages"]]
    for want in ("RelevanceGate", "CandidatePool", "AnchorChannel",
                 "LayerSweep", "HydrationFilter", "TopK"):
        self.assertIn(want, names, f"missing stage {want}")
    for s in t["stages"]:
        self.assertIn(s["verdict"], ("found", "not_found", "degraded"))
        self.assertGreaterEqual(s["elapsed_ms"], 0.0)
    self.assertEqual(t["verdict"], r["recall_verdict"],
                     "trace verdict must equal top-level recall_verdict")

def test_trace_marks_degraded_channel_stage(self):
    self.fx.vector.fail = True
    r = self.fx.search(include_trace=True)
    pool = next(s for s in r["recall_trace"]["stages"]
                if s["stage"] == "CandidatePool")
    self.assertEqual(pool["verdict"], "degraded")
    self.assertEqual(pool["detail"]["channels"]["vector"], "degraded")
    self.assertEqual(r["recall_trace"]["verdict"], "degraded")

def test_trace_empty_query_reports_gate_skip(self):
    r = self.fx.search(text="   ", include_trace=True)
    t = r["recall_trace"]
    self.assertTrue(t["skipped"])
    self.assertEqual(t["stages"][0]["stage"], "RelevanceGate")
    self.assertEqual(t["stages"][0]["verdict"], "not_found")
```

Note: the fixture's `search()` helper must accept and forward `include_trace` and `text` kwargs into `QueryRequest` — extend the copied helper accordingly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_recall_funnel_trace.py -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'include_trace'` (Part A tests still pass)

- [ ] **Step 3: Write minimal implementation**

3a. `QueryRequest` — add field after `depth` (~line 96):

```python
    #: 1.3.0 ③-1 RECALL funnel: attach per-stage trace to the response.
    #: Default off (perf) — API exposes via ?trace=true.
    include_trace: bool = False
```

3b. `search()` — at the top after `candidate_limit` is computed (~line 347), add:

```python
        from .recall_trace import RecallStage, RecallTrace
        _t0 = time.monotonic() if request.include_trace else 0.0
        _stages: list = []

        def _stage(name: str, verdict: str, hits: int, detail: dict | None = None) -> None:
            _stages.append(RecallStage(
                name=name, verdict=verdict, hits=hits,
                elapsed_ms=round((time.monotonic() - _t0) * 1000, 2),
                detail=detail or {}))
```

3c. Empty-query early return (~line 324-328) — when `request.include_trace`, return trace with one gate stage instead of bare dict:

```python
        if not query:
            base = {
                "results": [], "total": 0, "filtered": {"acl": 0, "status": 0},
                "gate": {"skipped": True, "reason": "empty query"},
            }
            if request.include_trace:
                base["recall_trace"] = RecallTrace(
                    skipped=True, verdict="not_found", degraded=False,
                    lanes={}, stages=[RecallStage(
                        name="RelevanceGate", verdict="not_found", hits=0,
                        elapsed_ms=0.0, detail={"reason": "empty query"})]).to_dict()
            return base
```

3d. Gate-skip early return (~line 337-341) — same pattern: attach single `RelevanceGate` stage with `verdict="not_found"`, trace `verdict="not_found"`, `detail={"reason": reason}`.

3e. After channels+anchor+sweep block (~line 397, before `results = []`): record channel/anchor/sweep stages:

```python
        if request.include_trace:
            _pool_degraded = any(s == "degraded" or s == "open"
                                 for s in channel_states.values())
            _stage("RelevanceGate", "found", 1, {"reason": reason})
            _stage("CandidatePool",
                   "degraded" if _pool_degraded
                   else ("found" if ranked else "not_found"),
                   len(ranked),
                   {"channels": dict(channel_states),
                    "per_channel_hits": {c: len(v.get("channels", [])) and 0 or 0 for c, v in []} or {}})
```

Simplify the detail — per-channel hit counts are not tracked per channel in `ranked` (it merges). Use what exists: `{"channels": dict(channel_states)}` only. Do NOT invent per-channel hit bookkeeping (YAGNI; the merged pool count + states is the honest signal).

```python
        if request.include_trace:
            _pool_degraded = any(s == "degraded" or s == "open"
                                 for s in channel_states.values())
            _stage("RelevanceGate", "found", 1, {"reason": reason})
            _stage("CandidatePool",
                   "degraded" if _pool_degraded
                   else ("found" if ranked else "not_found"),
                   len(ranked), {"channels": dict(channel_states)})
            _stage("AnchorChannel",
                   "found" if anchor_injected else "not_found",
                   anchor_injected)
            _stage("LayerSweep",
                   "found" if sweep_injected else "not_found",
                   sweep_injected, {"depth": request.depth})
```

3f. After verdict is computed (~line 489-494), before `return {`, add:

```python
        if request.include_trace:
            _stage("HydrationFilter",
                   "degraded" if degraded
                   else ("found" if results else "not_found"),
                   len(results), {"filtered": dict(filtered)})
            _stage("TopK",
                   "found" if top_results else "not_found",
                   len(top_results), {"limit": request.limit})
```

3g. In the return dict (~line 496-517), add after `"degraded": degraded,`:

```python
            **({"recall_trace": RecallTrace(
                skipped=False, verdict=verdict, degraded=degraded,
                lanes=self.lane_profile(request).describe(),
                stages=_stages).to_dict()} if request.include_trace else {}),
```

Note: `lane_profile()` exists at `query.py:284` and `describe()` at `:165` — verify `describe()` returns a dict (read lines 161-180 before using; if it returns str, use `lanes={"profile": self.lane_profile(request).describe()}` and adjust the Task 1 test accordingly — do NOT change `LaneProfile`).

Gate-error-fallback honesty: the `except Exception` at ~line 334 sets `reason = "gate-error-fallback"` and proceeds. The RelevanceGate stage must report `verdict="degraded"` in that case: implement as `"degraded" if reason == "gate-error-fallback" else "found"` in step 3e. (The gate faulted but search proceeded — the stage tells the truth.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recall_funnel_trace.py tests/test_p46_resilience_tier.py tests/test_p0a_recall_verdict.py -v`
Expected: all PASS (no regression in existing verdict/channel tests)

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/query.py tests/test_recall_funnel_trace.py
git commit -m "feat(1.3.0-③-1): search() RECALL funnel instrumentation — six stages with per-stage verdicts, trace only when include_trace"
```

---

### Task 3: `?trace=true` on POST /v8/query

**Files:**
- Modify: `mimir_v8/api.py` (`query_v8` at line 906-908; `QueryBody` untouched)
- Test: `tests/test_r7_api.py` (append; read its existing query-endpoint test setup first) — or extend `tests/test_recall_funnel_trace.py` with an API-level class if `test_r7_api.py` fixtures are heavy. Prefer appending to `test_recall_funnel_trace.py` to keep the feature's tests in one place; use FastAPI TestClient against the app object exactly as `test_r7_api.py` does (copy its client-construction lines, do not reinvent).

**Interfaces:**
- Consumes: `search()` trace support from Task 2.
- Produces: `POST /v8/query?trace=true` → response contains `recall_trace`; without param → byte-identical shape to before.

- [ ] **Step 1: Write the failing test**

```python
def test_query_trace_param_returns_trace(self):
    r = self.client.post("/v8/query?trace=true",
                         json={"text": "生产库 API 写入 铁律", "limit": 5})
    self.assertEqual(r.status_code, 200)
    body = r.json()
    self.assertIn("recall_trace", body)
    self.assertEqual(body["recall_trace"]["verdict"],
                     body["recall_verdict"])

def test_query_without_trace_param_unchanged(self):
    r = self.client.post("/v8/query",
                         json={"text": "生产库 API 写入 铁律", "limit": 5})
    self.assertEqual(r.status_code, 200)
    self.assertNotIn("recall_trace", r.json())
```

(Fixture: `self.client` = TestClient built as in `test_r7_api.py`; seed at least one readable fact so verdict is `found`; principal must hold `read` scope — copy the auth header construction from `test_r7_api.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recall_funnel_trace.py -v -k "trace_param or without_trace"`
Expected: FAIL — `recall_trace` missing even with `?trace=true` (param not wired)

- [ ] **Step 3: Write minimal implementation** — modify `query_v8` (`api.py:906-908`):

```python
    @app.post("/v8/query")
    def query_v8(body: QueryBody, identity: Principal = Depends(scoped("read")),
                 trace: bool = Query(default=False)):
        return execute_query(body, identity, include_trace=trace)
```

And `execute_query` (`api.py:721`) gains keyword-only param:

```python
    def execute_query(body: QueryBody, identity: Principal,
                      *, include_trace: bool = False) -> dict:
```

passing `include_trace=include_trace` into the `QueryRequest(...)` constructor (after `depth=body.depth,`). Check `Query` is imported in api.py (it is — used at line 911 `Query(default=10...)`).

Other `execute_query` callers (lines 1478, 1955) keep working unchanged (default False).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recall_funnel_trace.py tests/test_r7_api.py tests/test_p46_resilience_tier.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/api.py tests/test_recall_funnel_trace.py
git commit -m "feat(1.3.0-③-1): POST /v8/query?trace=true — funnel trace opt-in, default response shape unchanged"
```

---

## Self-Review

**Spec coverage (§三 ③-1):**
- "候选池→三路召回→去重→时间衰减→最终每步耗时与命中数" → stages RelevanceGate/CandidatePool/AnchorChannel/LayerSweep/HydrationFilter/TopK with hits + elapsed_ms. Note: spec's "去重/时间衰减" wording predates the discovery that `search()` has no Jaccard-dedup stage (dedup lives only in v12 `trace()`); this plan instruments the real search() pipeline instead of inventing stages — decay signal surfaces via HydrationFilter detail + per-result `decay_factor` already present. If the user wants literal Dedup/ChronosDecay stages on the search path, that is a spec amendment, not a plan gap.
- "?trace=true, 默认不返" → Task 3 + `include_trace` default False.
- "degraded 显式标红" → per-stage degraded verdicts + channel states in detail (Task 2).
- "任一通道 mock 故障 → 对应步 degraded 且最终 degraded" → Task 2 test 3.

**Placeholder scan:** no TBD/TODO; every step has exact code, exact run commands, exact expected outputs. The one intentional branch (LaneProfile.describe() return type) has an explicit read-then-adapt instruction with a fallback that does not change LaneProfile.

**Type consistency:** `RecallStage(...).to_dict()` shape asserted in Task 1 tests and consumed in Task 2 step 3g; `QueryRequest.include_trace: bool` set in Task 2, read in Task 3 via `execute_query(..., include_trace=trace)`. Names match across tasks.
