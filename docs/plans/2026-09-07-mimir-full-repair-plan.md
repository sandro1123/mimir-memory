# Mímir v14.1.0 Full Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, this session — Mímir work is never delegated to other sessions). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate every P0/P1 finding of the 2026-09-07 audit (report: `C:\Users\sandr\mimir_audit_20260907\Mimir-全面审计-2026-09-07.md`, vault `分层讨论/Mímir-全面审计-2026-09-07.md`) and ship it as v14.1.0 to the production device n100-ai, plus the safe P2/P3 hygiene items.

**Architecture:** Code fixes land on branch `v14.1.0` in `C:\mimir-work\mimir` (TDD, one commit per task), are regression-tested on the device against a throwaway worktree, then released with the established recipe (tag → `git archive` release tree → cloned venv → 18 systemd units + 4 ops scripts re-pointed → restart API/dashboard). Production data repairs (re-projection of 3 disputed facts, human_status backfill) go through code paths that emit events, never raw UPDATEs. Ops-side fixes (health checks, offsite backup, unit env) live in the production git repo `~/.hermes/mimir`.

**Tech Stack:** Python 3.11 (device venv), FastAPI, SQLite (WAL), pytest/unittest, systemd, bash, gpg, Alpine.js frontend.

**Spec:** the audit report §1–§8 (paths above).

## Global Constraints

- Repo-local `core.autocrlf=false`; all `mimir_v8/*.py`, `dashboard/**`, `hermes-plugin/**`, `tests/*.py` are LF. `CHANGELOG.md` is CRLF and must stay CRLF. After every edit run `git diff --numstat` and confirm no whole-file rewrite.
- `SCHEMA_VERSION` stays **20**. No DDL changes. `MIMIR_VERSION` → **14.1.0** in `mimir_v8/schema.py`, `pyproject.toml`, `tests/test_r8_release.py`.
- Never `git stash`; never push to `master` on the device while its tree is dirty (it is clean except `tmp_sync/`, which Task 13 deletes).
- Local full-suite runs must `--ignore=tests/test_r8_release.py` (py3.10 has no `tomllib`) and are only indicative; the device run is authoritative. Known pre-existing device errors: `tests/test_v12_insight.py::TestM1dHermesPluginContract` ×3.
- Production writes only via: systemd/ops files, the REST API with `admin.token`, or the new `reproject` CLI while `mimir.service` is stopped. Zero raw SQL writes to `canonical.db`.
- Do not restart Hermes gateways (`hermes-gateway.service`, `hermes-gateway-{jarvis,mentor,quantmaster}`); plugin changes take effect at their next restart — say so in the report.

---

### Task 0: Branch and land the two in-flight items

**Files:** `mimir_v8/worker.py`, `tests/test_p23_vault_knowledge_routing.py` (item A); `mimir_v8/api.py`, `mimir_v8/schema.py`, `mimir_v8/store.py`, `mimir_v8/eval_suite.py`, `tests/test_p36_golden_stewardship.py` (item B).

- [ ] **Step 1:** `cd C:\mimir-work\mimir && git checkout -b v14.1.0`
- [ ] **Step 2:** Run `python -m pytest tests/test_p23_vault_knowledge_routing.py tests/test_p36_golden_stewardship.py tests/test_p19_ingestion_pipeline.py tests/test_r9_eval.py -q -p no:cacheprovider` → expect all pass (20 + 17 + r9).
- [ ] **Step 3:** `git add mimir_v8/worker.py tests/test_p23_vault_knowledge_routing.py && git commit -m "feat(collector): #41-A vault notes dual-route into wiki knowledge layer"`
- [ ] **Step 4:** `git add mimir_v8/api.py mimir_v8/schema.py mimir_v8/store.py mimir_v8/eval_suite.py tests/test_p36_golden_stewardship.py && git commit -m "feat(eval): golden-set health sentinel (--golden-health) + UpdateFact.decay_tier"`
- [ ] **Step 5:** `git status --short` → only `docs/plans/2026-09-07-mimir-full-repair-plan.md` untracked; commit it: `git add docs/plans/2026-09-07-mimir-full-repair-plan.md && git commit -m "docs(plans): v14.1.0 full repair plan"`.

### Task 1: Governance — env fallback, honest LLM errors, requeue cap, worker exit codes

**Files:** Modify `mimir_v8/governance.py:25-31, 95-108, 141-150`; Modify `mimir_v8/worker.py:612-671` (review_requeue), `:355-395` (yaml loads), `main()` tail (`return 0`), governance branch; Test: `tests/test_p47_governance_resilience.py` (new).

**Interfaces:**
- Produces `governance.router_config() -> dict(url, api_key, primary_model, fallback_model)`; `governance._call_llm(prompt, model) -> tuple[dict|None, str|None]`; `worker.review_requeue(..., max_failed_24h: int = 3)`; `worker._exit_code(result) -> int`; governance command result gains `llm_failures: int`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_p47_governance_resilience.py
# -*- coding: utf-8 -*-
"""P47 治理链韧性：环境变量回退 / LLM 失败不再无声 / requeue 上限 / worker 退出码。

审计 2026-09-07 P0-1：治理单元从未配 MIMIR_ROUTER_*，抽取单元只有 MIMIR_EVALUATOR_*，
v10 硬编码 key 被开源脱敏后治理静默失效 6 天，18 条候选每 15 分钟被 requeue 一轮。
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mimir_v8 import governance
from mimir_v8 import worker
from mimir_v8.store import CanonicalStore, utc_now


def _insert_candidate(store, cid, status="human_review"):
    now = utc_now()
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_facts(candidate_id,content,summary,proposed_owner_principal,"
            "proposed_domain,proposed_fact_type,uncertainty_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, f"content {cid}", f"summary {cid}", "mentor", "system", "pattern", "[]", status, now, now),
        )


def _insert_failed_assessment(store, cid, created_at):
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_review_assessments(assessment_id,candidate_id,reviewer_type,provider,"
            "model,recommendation,risk,confidence,is_valuable,is_noise,domain,fact_type,summary,reasoning,"
            "raw_output_hash,success,error_code,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"a-{cid}-{created_at}", cid, "llm", "router", "", "unknown", "medium", 0.0, 0, 0,
             "personal", "user_pref", "", "", "", 0, "LLM 不可用", created_at),
        )


class TestRouterConfigFallback(unittest.TestCase):
    def test_falls_back_to_evaluator_env(self):
        env = {"MIMIR_EVALUATOR_API_URL": "http://127.0.0.1:20128/v1",
               "MIMIR_EVALUATOR_API_KEY": "sk-test", "MIMIR_EVALUATOR_MODEL": "m/x"}
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = governance.router_config()
        self.assertEqual(cfg["url"], "http://127.0.0.1:20128/v1")
        self.assertEqual(cfg["api_key"], "sk-test")
        self.assertEqual(cfg["primary_model"], "m/x")
        self.assertEqual(cfg["fallback_model"], "m/x")

    def test_router_env_wins(self):
        env = {"MIMIR_ROUTER_API_KEY": "sk-router", "MIMIR_EVALUATOR_API_KEY": "sk-eval",
               "MIMIR_GOVERNANCE_MODEL": "gov", "MIMIR_EVALUATOR_MODEL": "eva"}
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = governance.router_config()
        self.assertEqual(cfg["api_key"], "sk-router")
        self.assertEqual(cfg["primary_model"], "gov")


class TestLlmErrorsAreHonest(unittest.TestCase):
    def test_missing_key_reports_cause(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result, error = governance._call_llm("p", "m")
        self.assertIsNone(result)
        self.assertIn("api_key", error)

    def test_transport_error_reports_cause(self):
        with mock.patch.dict("os.environ", {"MIMIR_ROUTER_API_KEY": "sk-x"}, clear=True), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("connection refused")), \
             self.assertLogs("mimir_v8.governance", level="WARNING") as logs:
            result, error = governance._call_llm("p", "m")
        self.assertIsNone(result)
        self.assertIn("OSError", error)
        self.assertTrue(any("connection refused" in line for line in logs.output))

    def test_assess_candidate_error_carries_cause(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            a = governance.assess_candidate("普通内容 no markers", "cid-1")
        self.assertFalse(a.success)
        self.assertTrue(a.error.startswith("LLM 不可用"))
        self.assertIn("api_key", a.error)


class TestRequeueCap(unittest.TestCase):
    def test_only_unassessed_skips_candidates_over_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            _insert_candidate(store, "fresh")
            _insert_candidate(store, "worn")
            recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            for i in range(3):
                _insert_failed_assessment(store, "worn", recent[:-1] + str(i))
            old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
            _insert_candidate(store, "rested")
            for i in range(5):
                _insert_failed_assessment(store, "rested", old[:-1] + str(i))
            r = worker.review_requeue(store, "service:governance", only_unassessed=True)
            self.assertEqual(r["requeued"], 2)
            with store.connect() as c:
                rows = dict(c.execute("SELECT candidate_id, status FROM candidate_facts").fetchall())
            self.assertEqual(rows["fresh"], "review_required")
            self.assertEqual(rows["rested"], "review_required")
            self.assertEqual(rows["worn"], "human_review")


class TestWorkerExitCode(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(worker._exit_code({"created": [], "failed": []}), 0)
        self.assertEqual(worker._exit_code({"errors": [{"source": "x"}]}), 1)
        self.assertEqual(worker._exit_code({"failed": [{"run_id": "r"}]}), 1)
        self.assertEqual(worker._exit_code({"llm_failures": 3, "processed": 3}), 1)
        self.assertEqual(worker._exit_code({"llm_failures": 0, "processed": 3}), 0)
        self.assertEqual(worker._exit_code("not a dict"), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2:** Run `python -m pytest tests/test_p47_governance_resilience.py -q -p no:cacheprovider` → expect FAIL (`router_config` missing, `_call_llm` returns single value, `_exit_code` missing, requeue count 3 not 2).
- [ ] **Step 3: Implement in `mimir_v8/governance.py`**

```python
import logging
logger = logging.getLogger("mimir_v8.governance")

def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def router_config() -> dict:
    """LLM router config resolved at call time.

    2026-09-07 audit P0-1: the governance systemd unit only ever carried the
    evaluator's ``MIMIR_EVALUATOR_*`` variables; the ``MIMIR_ROUTER_*`` names
    were never configured in production, so after the open-source release
    sanitized the hard-coded defaults governance ran keyless for six days.
    Fall back to the evaluator variables so one env file serves both workers.
    """
    primary = _env_first("MIMIR_GOVERNANCE_MODEL", "MIMIR_EVALUATOR_MODEL", default="default-model")
    return {
        "url": _env_first("MIMIR_ROUTER_URL", "MIMIR_EVALUATOR_API_URL", default="http://127.0.0.1:20128/v1").rstrip("/"),
        "api_key": _env_first("MIMIR_ROUTER_API_KEY", "MIMIR_EVALUATOR_API_KEY"),
        "primary_model": primary,
        "fallback_model": _env_first("MIMIR_GOVERNANCE_FALLBACK_MODEL", default=primary),
    }
```
Replace the four module constants by `router_config()` lookups: keep `GOVERNANCE_AUTO_APPROVE`/`GOVERNANCE_FAST_TRACK_THRESHOLD` as-is; delete `ROUTER_URL/ROUTER_API_KEY/PRIMARY_MODEL/FALLBACK_MODEL` constants.

```python
def _call_llm(prompt: str, model: str) -> tuple[dict | None, str | None]:
    """Return (parsed_json, error). error is a short cause string, never None on failure."""
    cfg = router_config()
    if not cfg["api_key"]:
        cause = "api_key missing (set MIMIR_ROUTER_API_KEY or MIMIR_EVALUATOR_API_KEY)"
        logger.warning("governance LLM skipped: %s", cause)
        return None, cause
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 1024}
    try:
        req = urllib.request.Request(f"{cfg['url']}/chat/completions", data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = json.loads(resp.read())["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None, "no JSON object in model output"
        return json.loads(match.group()), None
    except Exception as exc:  # noqa: BLE001 - cause is surfaced, not swallowed
        cause = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning("governance LLM call failed model=%s: %s", model, cause)
        return None, cause
```
In `assess_candidate`: 
```python
    cfg = router_config()
    llm_result, cause = _call_llm(prompt, cfg["primary_model"])
    model_used = cfg["primary_model"]
    if llm_result is None and cfg["fallback_model"] != cfg["primary_model"]:
        llm_result, cause2 = _call_llm(prompt, cfg["fallback_model"])
        model_used = cfg["fallback_model"]
        cause = f"{cause}; fallback: {cause2}" if cause2 else cause
    if llm_result is None:
        result.error = f"LLM 不可用: {cause}"[:240]
        result.risk = "medium"
        return result
```
- [ ] **Step 4: Implement in `mimir_v8/worker.py`**
  - Add `import logging` and `logger = logging.getLogger("mimir_v8.worker")` at module top (after existing imports).
  - Both yaml `except Exception:` blocks (`_load_config_feeds`, `load_source_registry`) become `except Exception as exc: logger.warning("mimir_config.yaml unreadable at %s: %s", config_path, exc); return []`.
  - `review_requeue(store, actor_principal, *, dry_run=False, only_unassessed=False, max_failed_24h: int = 3)`; when `only_unassessed`, build:
```python
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        sql += (" AND NOT EXISTS (SELECT 1 FROM candidate_review_assessments a"
                " WHERE a.candidate_id=candidate_facts.candidate_id AND a.success=1)"
                # P47 cap: a candidate that already failed N LLM assessments in the last
                # 24h stays in human_review (a human can still act) instead of bouncing
                # every 15 minutes while the router is down (7836 requeue events/7d).
                " AND (SELECT COUNT(*) FROM candidate_review_assessments f"
                " WHERE f.candidate_id=candidate_facts.candidate_id AND f.success=0"
                " AND f.created_at >= ?) < ?")
        params = (cutoff, max_failed_24h)
```
    and pass `params` to `connection.execute(sql, params)` (empty tuple otherwise). Import `datetime, timedelta, timezone` from `datetime` if not already.
  - governance branch: after `result = run_governance_once(...)` add `result["llm_failures"] = sum(1 for r in result.get("results", []) if not r["assessment"]["success"] and str(r["decision"]["reason"]).startswith("LLM"))`.
  - Add before `main`:
```python
def _exit_code(result) -> int:
    """systemd sees failures: any recorded error → 1 (audit 2026-09-07 P1-11)."""
    if not isinstance(result, dict):
        return 0
    if result.get("errors") or result.get("failed") or result.get("fast_track_errors"):
        return 1
    if result.get("llm_failures"):
        return 1
    if result.get("status") == "error":
        return 1
    return 0
```
    and change the final `return 0` in `main()` to `return _exit_code(result)`.
- [ ] **Step 5:** Run the new test file + `tests/test_p22_collector_wiring.py tests/test_p19_ingestion_pipeline.py` and any existing test that imports governance (`grep -l "governance" tests/*.py`) → all pass.
- [ ] **Step 6:** `git add mimir_v8/governance.py mimir_v8/worker.py tests/test_p47_governance_resilience.py && git commit -m "fix(governance): P47 env fallback to MIMIR_EVALUATOR_*, honest LLM error causes, requeue cap, worker exit codes"`

### Task 2: Wire the P45 `error_hook`

**Files:** Modify `mimir_v8/runtime.py:191`; Test: append to `tests/test_p45_supervisor_resilience.py`.

- [ ] **Step 1: Failing test** (append this pytest-style function to the file):
```python
def test_build_runtime_wires_error_hook(tmp_path):
    """审计 2026-09-07 P1-8：P45 加了 error_hook 参数，但 build_runtime 从未传入。"""
    import hashlib, json
    from mimir_v8.runtime import build_runtime
    token = "hook-token"
    token_path = tmp_path / "tokens.json"
    token_path.write_text(json.dumps({"principals": [{"id": "mentor",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "scopes": ["read"], "roles": [], "admin": False}]}), encoding="utf-8")
    _app, components = build_runtime(tmp_path / "data", token_path, vector_enabled=False, start_supervisor=False)
    supervisor = components["supervisor"]
    assert supervisor.error_hook is not None
    # the hook must log, not raise
    supervisor.error_hook("fts", RuntimeError("boom"))
```
  If `components` has no `supervisor` key, inspect `runtime.py` `components = {...}` and add `"supervisor": supervisor` to it.
- [ ] **Step 2:** Run → FAIL (`error_hook is None`).
- [ ] **Step 3:** In `runtime.py` add `import logging` + `logger = logging.getLogger("mimir_v8.runtime")` and:
```python
def _log_projector_error(name: str, exc: BaseException) -> None:
    logger.error("projector %s raised %s: %s", name, type(exc).__name__, exc)

    supervisor = ProjectorSupervisor(runners, error_hook=_log_projector_error)
```
- [ ] **Step 4:** Run `tests/test_p45_supervisor_resilience.py tests/test_p31*.py` → pass.
- [ ] **Step 5:** `git commit -am "fix(runtime): wire P45 error_hook so projector failures are logged (audit P1-8)"`

### Task 3: Human approvals produce `human_status=confirmed`

**Files:** Modify `mimir_v8/candidates.py:288-300`; Test: `tests/test_p47_review_semantics.py` (new).

- [ ] **Step 1: Failing test**
```python
# tests/test_p47_review_semantics.py
"""审计 2026-09-07 P1-3：候选 approve 落库后事实仍 unreviewed（389/410）。
人审（非 service:*）批准 → 事实 confirmed；治理自动批准 → 仍 unreviewed。"""
import tempfile, unittest
from pathlib import Path
from mimir_v8.candidates import CandidateService, ReviewCandidate
from mimir_v8.store import CanonicalStore, utc_now


def _insert_candidate(store, cid):
    now = utc_now()
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_facts(candidate_id,content,summary,proposed_owner_principal,"
            "proposed_domain,proposed_fact_type,uncertainty_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cid, f"content {cid}", f"summary {cid}", "mentor", "system", "pattern", "[]", "review_required", now, now))


class TestApproveSetsHumanStatus(unittest.TestCase):
    def _commit(self, reviewer):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        store = CanonicalStore(Path(tmp.name) / "canonical.db")
        svc = CandidateService(store)
        _insert_candidate(store, "c1")
        svc.review_candidate(ReviewCandidate(candidate_id="c1", action="approve", reason="ok", idempotency_key="ik-1"), reviewer)
        out = svc.commit_approved("c1", actor_principal=reviewer, idempotency_key="ik-2")
        return store.get_fact(out["fact_id"])

    def test_human_reviewer_confirms(self):
        self.assertEqual(self._commit("admin")["human_status"], "confirmed")

    def test_service_reviewer_stays_unreviewed(self):
        self.assertEqual(self._commit("service:governance")["human_status"], "unreviewed")
```
- [ ] **Step 2:** Run → FAIL (`confirmed != unreviewed`). If `review_candidate` requires the candidate in a specific status, adjust the inserted status to what `candidates.py:228` accepts (`review_required` is in the list).
- [ ] **Step 3:** In `commit_approved`, before `CreateFact(...)`:
```python
        reviewer = str(candidate["reviewed_by"] or "")
        # audit 2026-09-07 P1-3: a human approval is the human review; only
        # automatic (service:*) approvals leave the fact unreviewed.
        human_status = "confirmed" if reviewer and not reviewer.startswith("service:") else "unreviewed"
```
  and add `human_status=human_status,` to the `CreateFact(...)` kwargs.
- [ ] **Step 4:** Run new test + `tests/test_r7_api.py tests/test_v10*.py` (whatever exists that covers candidates) → pass.
- [ ] **Step 5:** `git add -A mimir_v8/candidates.py tests/test_p47_review_semantics.py && git commit -m "fix(candidates): human approval commits facts as human_status=confirmed (audit P1-3)"`

### Task 4: Hermes plugin — sync from production, per-agent token, honest errors, deterministic idempotency, reflect body

**Files:** Overwrite `hermes-plugin/mimir_memory_provider/__init__.py` and `tools.py` from device `~/.hermes/plugins/mimir_memory_provider/` (production is newer); then modify `tools.py`; `plugin.yaml` version → 14.1.0; Test: `tests/test_p47_plugin_tools.py` (new, imports tools.py via importlib — no Hermes dependency).

- [ ] **Step 1:** `scp mimir-n100:~/.hermes/plugins/mimir_memory_provider/{__init__.py,tools.py} C:/mimir-work/mimir/hermes-plugin/mimir_memory_provider/` then `git diff --stat` (expect ~90 changed lines, LF preserved: `file hermes-plugin/mimir_memory_provider/tools.py` shows no CRLF).
- [ ] **Step 2: Failing tests**
```python
# tests/test_p47_plugin_tools.py
"""审计 2026-09-07 P1-2/P1-10：插件读路径全 admin.token；remember 失败返 {}；幂等键 hash() 随机；reflect 发 query 而非 text。"""
import importlib.util, unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "hermes-plugin" / "mimir_memory_provider" / "tools.py"


def _load():
    spec = importlib.util.spec_from_file_location("mimir_plugin_tools_p47", str(TOOLS))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class TestPluginTools(unittest.TestCase):
    def test_idempotency_key_is_deterministic(self):
        t = _load()
        self.assertEqual(t._idempotency_key("mentor", "abc"), t._idempotency_key("mentor", "abc"))
        self.assertNotEqual(t._idempotency_key("mentor", "abc"), t._idempotency_key("jarvis", "abc"))
        self.assertTrue(t._idempotency_key("mentor", "abc").startswith("plugin-remember:"))

    def test_reflect_posts_text_field(self):
        t = _load(); seen = {}
        with mock.patch.object(t, "_post", side_effect=lambda p, d: seen.setdefault("body", d) or {"results": []}):
            t.mimir_reflect("topic x")
        self.assertEqual(seen["body"], {"text": "topic x", "limit": 10})

    def test_remember_surfaces_error(self):
        t = _load()
        with mock.patch.object(t, "_post", return_value={"error": {"kind": "unreachable", "detail": "refused"}}):
            out = t.mimir_remember("x", owner="mentor")
        self.assertFalse(out["ok"]); self.assertIn("refused", out["error"])

    def test_search_returns_empty_on_error_dict(self):
        t = _load()
        with mock.patch.object(t, "_post", return_value={"error": {"kind": "http", "status": 401}}):
            self.assertEqual(t.mimir_search("q"), [])

    def test_token_prefers_agent_token(self):
        t = _load()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "clients"; d.mkdir()
            (d / "admin.token").write_text("ADMIN\n"); (d / "jarvis.token").write_text("JARVIS\n")
            with mock.patch.dict("os.environ", {"MIMIR_PLUGIN_TOKEN_DIR": str(d)}, clear=False), \
                 mock.patch.object(t, "_owner", return_value="jarvis"):
                t.ADMIN_TOKEN_FILE = d / "admin.token"
                self.assertEqual(t._token(), "JARVIS")
            with mock.patch.dict("os.environ", {"MIMIR_PLUGIN_TOKEN_DIR": str(d)}, clear=False), \
                 mock.patch.object(t, "_owner", return_value="mentor"):
                self.assertEqual(t._token(), "ADMIN")  # no mentor.token → admin fallback
            with mock.patch.dict("os.environ", {"MIMIR_PLUGIN_TOKEN_DIR": str(d), "MIMIR_PLUGIN_TOKEN_FILE": str(d / "admin.token")}, clear=False), \
                 mock.patch.object(t, "_owner", return_value="jarvis"):
                self.assertEqual(t._token(), "ADMIN")  # explicit file wins
```
- [ ] **Step 3:** Run → FAIL.
- [ ] **Step 4: Implement in `tools.py`**
```python
import hashlib, logging
logger = logging.getLogger("mimir_memory_provider")
TOKEN_DIR_DEFAULT = Path.home() / ".hermes/mimir/secrets/clients"
ADMIN_TOKEN_FILE = Path(os.environ.get("MIMIR_PLUGIN_TOKEN_FILE", str(TOKEN_DIR_DEFAULT / "admin.token")))


def _token() -> str | None:
    """Per-agent token so Mímir ACL applies to reads too (audit 2026-09-07 P1-2).

    Priority: explicit MIMIR_PLUGIN_TOKEN_FILE > <token dir>/<owner>.token > admin.token.
    """
    explicit = os.environ.get("MIMIR_PLUGIN_TOKEN_FILE", "").strip()
    if explicit and Path(explicit).exists():
        return Path(explicit).read_text().strip()
    token_dir = Path(os.environ.get("MIMIR_PLUGIN_TOKEN_DIR", str(TOKEN_DIR_DEFAULT)))
    agent_file = token_dir / f"{_owner()}.token"
    if agent_file.exists():
        return agent_file.read_text().strip()
    admin = token_dir / "admin.token"
    if admin.exists():
        return admin.read_text().strip()
    if ADMIN_TOKEN_FILE.exists():
        return ADMIN_TOKEN_FILE.read_text().strip()
    return None


def _error(kind: str, **detail) -> dict:
    logger.warning("mimir plugin %s: %s", kind, detail)
    return {"error": {"kind": kind, **detail}}


def _post(path: str, data: dict) -> dict | None:
    try:
        request = urllib.request.Request(f"{MIMIR_API}{path}", data=json.dumps(data).encode(), headers=_headers(), method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return _error("http", status=exc.code, path=path, body=exc.read().decode(errors="replace")[:300])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _error("unreachable", path=path, detail=str(exc)[:200])
```
  `_get` mirrors `_post`. Then:
```python
def _ok(result) -> bool:
    return isinstance(result, dict) and "error" not in result

def _idempotency_key(owner: str, content: str) -> str:
    digest = hashlib.sha256(f"{owner}\n{content}".encode("utf-8")).hexdigest()[:32]
    return f"plugin-remember:{digest}"

def mimir_search(query, limit=5, **kwargs):
    result = _post("/v8/query", {"text": query, "limit": limit})
    return result.get("results", []) if _ok(result) else []

def mimir_remember(content, owner="", domain="personal", fact_type="user_pref", **kwargs):
    principal = owner or _owner()
    result = _post("/v8/learning/remember", {"content": content, "owner_principal": principal,
                   "domain": domain, "fact_type": fact_type, "idempotency_key": _idempotency_key(principal, content)})
    if _ok(result):
        return {"ok": True, **result}
    err = (result or {}).get("error", {})
    return {"ok": False, "error": f"{err.get('kind','unknown')} {err.get('status','')} {err.get('detail', err.get('body',''))}".strip()}

def mimir_recent(limit=10, **kwargs):
    result = _get(f"/v8/memories/recent?limit={int(limit)}")
    return (result.get("results", result.get("memories", [])) if _ok(result) else [])

def mimir_reflect(topic="", **kwargs):
    if topic:
        result = _post("/v8/query", {"text": topic, "limit": 10})
        return {"topic": topic, "results": result.get("results", []) if _ok(result) else []}
    result = _get("/v12/evolve/report")
    return result if _ok(result) else None
```
  `mimir_feedback` unchanged. Update `__all__`. In `__init__.py`, `handle_tool_call` for `mimir_remember` already serializes the dict — verify it does not assume `{}` means success (read lines ~185-215; if it prints "stored", switch the message to reflect `out.get("ok")`).
- [ ] **Step 5:** `plugin.yaml` → `version: 14.1.0`, description mentions ACL-scoped tokens. Run tests → pass.
- [ ] **Step 6:** `git add hermes-plugin/mimir_memory_provider tests/test_p47_plugin_tools.py && git commit -m "fix(plugin): sync prod provider, per-agent tokens, honest errors, deterministic idempotency, reflect uses text (audit P1-2/P1-10)"`

### Task 5: Dashboard — move to `backend/`, fix routes, invalidate-all, session secret, error visibility

**Files:** `git mv dashboard/main.py dashboard/backend/main.py`, `git mv dashboard/requirements.txt dashboard/backend/requirements.txt`, `git rm dashboard/governance.py`; add `dashboard/backend/__init__.py` (empty); Modify `dashboard/backend/main.py` (cache `_invalidate_all`, review route, v10 decorators, v11 route, session secret, logging in `_mimir_get/_post/_db_query`); Modify `dashboard/frontend/index.html` (`fetchJSON` + `netErr` banner); Modify `tests/test_p41_dashboard_aggregates.py:19` path; Test: `tests/test_p47_dashboard_routes.py` (new).

- [ ] **Step 1:** `git mv` the three files; create empty `dashboard/backend/__init__.py`; `git rm dashboard/governance.py`; update `tests/test_p41_dashboard_aggregates.py:19` to `parents[1] / "dashboard" / "backend" / "main.py"`. Run `python -m pytest tests/test_p41_dashboard_aggregates.py -q -p no:cacheprovider` → pass (8).
- [ ] **Step 2: Failing tests**
```python
# tests/test_p47_dashboard_routes.py
"""审计 2026-09-07 P1-9/P1-12：看板路由与缓存/会话密钥缺陷。"""
import hashlib, hmac, importlib.util, inspect, time, unittest
from pathlib import Path
from unittest import mock

MAIN = Path(__file__).resolve().parents[1] / "dashboard" / "backend" / "main.py"
INDEX = Path(__file__).resolve().parents[1] / "dashboard" / "frontend" / "index.html"


def _load():
    spec = importlib.util.spec_from_file_location("dash_main_p47", str(MAIN))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.routes = {r.path: r for r in self.mod.app.routes if hasattr(r, "methods")}

    def test_v11_offload_registered(self):
        self.assertIn("/v11/symbolic/offload", self.routes)
        self.assertEqual(self.routes["/v11/symbolic/offload"].methods, {"POST"})

    def test_helper_not_exposed_as_route(self):
        self.assertNotIn("/v10/opinions", self.routes)
        self.assertNotIn("/v10/observations", self.routes)

    def test_review_is_post_only(self):
        self.assertEqual(self.routes["/api/candidates/{candidate_id}/review"].methods, {"POST"})

    def test_frontend_dir_resolves(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os; os.environ.pop("FRONTEND_DIR", None)
            mod = _load()
        self.assertTrue((mod.FRONTEND_DIR / "index.html").exists(), mod.FRONTEND_DIR)


class TestCacheInvalidation(unittest.TestCase):
    WRITE_ENDPOINTS = ("api_review_candidate", "api_commit_candidate", "api_conflict_resolve", "api_conflict_dismiss",
                       "api_crystals_scan", "api_crystals_approve", "api_crystals_dismiss", "api_governance_run", "api_skills_promote")

    def test_invalidate_all_clears_everything(self):
        mod = _load(); mod._cache.update({"facts": (0, 1), "dash_today:x=1": (0, 2), "skills": (0, 3)})
        mod._invalidate_all(); self.assertEqual(mod._cache, {})

    def test_every_write_endpoint_invalidates_all(self):
        mod = _load()
        for name in self.WRITE_ENDPOINTS:
            src = inspect.getsource(getattr(mod, name))
            self.assertIn("_invalidate_all()", src, name)


class TestSessionSecret(unittest.TestCase):
    def test_empty_password_hash_does_not_yield_public_secret(self):
        mod = _load()
        with mock.patch.object(mod, "_stored_password_hash", return_value=""):
            expiry = str(int(time.time()) + 3600)
            public_secret = hashlib.sha256(b"" + b"mimir-dashboard-session-v1").digest()
            forged = f"{expiry}.{hmac.new(public_secret, expiry.encode(), hashlib.sha256).hexdigest()}"
            self.assertFalse(mod._valid_session_token(forged))
            self.assertTrue(mod._valid_session_token(mod._make_session_token()))


class TestFrontendErrorVisibility(unittest.TestCase):
    def test_fetchjson_reports_errors(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("netErr", html)
        self.assertNotIn("if (!r.ok) return {};", html)
```
- [ ] **Step 3:** Run → FAIL (route missing, GET allowed, `_invalidate_all` missing, forged token accepted, html unchanged).
- [ ] **Step 4: Implement in `dashboard/backend/main.py`**
  - After `_invalidate`: 
```python
def _invalidate_all():
    """Any write may change any aggregate; writes are rare (human clicks), so
    dropping the whole cache is cheaper than maintaining per-endpoint lists
    (audit 2026-09-07: conflicts invalidated a non-existent key, crystals/
    governance-run invalidated nothing)."""
    _cache.clear()
```
    Replace every `_invalidate(...)` call in the nine write endpoints listed above with `_invalidate_all()`; add `_invalidate_all()` into `api_crystals_scan/approve/dismiss` and `api_governance_run` right after a 2xx response (before `return resp.json()`).
  - Line 629: `@app.api_route(..., methods=["GET", "POST"])` → `@app.post("/api/candidates/{candidate_id}/review")`.
  - Lines 1289-1290: delete the two `@app.get(...)` decorators above `_mimir_get_params`.
  - Line 1511: split into a comment line and `@app.post("/v11/symbolic/offload")` on its own line.
  - Session secret:
```python
import secrets as _secrets
_RUNTIME_SECRET = _secrets.token_bytes(32)

def _session_secret() -> bytes:
    stored = _stored_password_hash()
    if not stored:
        # audit 2026-09-07 P1-12: with no password configured the old key was a
        # public constant → forgeable cookies. Use a per-process random secret.
        return _RUNTIME_SECRET
    return hashlib.sha256(stored.encode() + b"mimir-dashboard-session-v1").digest()
```
  - `_mimir_get/_mimir_post`: on non-2xx `logger.warning("mimir api %s -> %s %s", path, resp.status_code, resp.text[:200])`; on exception `logger.warning("mimir api %s failed: %s", path, exc)`. `_db_query`: `except Exception as exc: logger.exception("dashboard db query failed: %s", exc); return []`. Add `import logging; logger = logging.getLogger("mimir_dashboard")` near the top.
  - Frontend `fetchJSON`:
```js
    async fetchJSON(url, opts={}) {
      try {
        const r = await fetch(url, {...opts, headers: this.authHeaders(opts.headers || {})});
        if (r.status === 401) { this.authed = false; return {}; }
        if (!r.ok) { this.netErr = `请求失败 ${r.status} · ${url}`; return {}; }
        this.netErr = '';
        return await r.json();
      } catch(e) { this.netErr = `网络错误 · ${url}`; return {}; }
    },
```
    add `netErr: '',` next to `conflictErr: '',` in the state block, and insert right after the `<body ...>` line:
    `<div x-show="netErr" x-text="netErr" x-cloak style="position:fixed;bottom:10px;right:10px;z-index:9999;background:#b91c1c;color:#fff;padding:6px 10px;border-radius:6px;font-size:12px;max-width:60vw"></div>`
- [ ] **Step 5:** Run `tests/test_p47_dashboard_routes.py tests/test_p41_dashboard_aggregates.py` → pass. `git diff --numstat dashboard/frontend/index.html` shows only ~8 changed lines (no CRLF rewrite).
- [ ] **Step 6:** Update `dashboard/README.md:15` "13 tabs" line → "**3 tabs · 客户视图**（今天 / 记忆库 / 设置）+ 开发者模式 14 面板"; `README.md:190` → `| Dashboard (3-tab customer view + 14-panel dev mode) | ✅ |`; `ARCHITECTURE.md:117` → "默认 3 个客户标签页（今天/记忆库/设置）+ 开发者模式 14 面板". Commit: `git add -A dashboard tests/test_p47_dashboard_routes.py tests/test_p41_dashboard_aggregates.py README.md ARCHITECTURE.md && git commit -m "fix(dashboard): backend/ layout matches deployment, v11 route, POST-only review, invalidate-all, random session secret, visible fetch errors (audit P1-7/P1-9/P1-12)"`

### Task 6: Projection drift repair — status-aware guard + `reproject` CLI

**Files:** Modify `mimir_v8/projector.py:100-108` (FTS guard); Modify `mimir_v8/operations.py` (add `reproject_facts`); Modify `mimir_v8/migrate_cli.py` (add `reproject` subcommand); Test: `tests/test_p47_reproject.py` (new).

**Interfaces:** `operations.reproject_facts(store, projectors: list, fact_ids: list[str]) -> dict(reprojected: list[str], missing: list[str], skipped_no_event: list[str])`. CLI: `python -m mimir_v8.migrate_cli reproject --data-dir <dir> --fact-ids a,b,c [--with-vector --collection NAME]`.

- [ ] **Step 1: Failing test**
```python
# tests/test_p47_reproject.py
"""审计 2026-09-07 P1-1：3 条 08 月 fact.conflict_lost 事实在投影里仍 active。
机理：FTS/graph apply 守卫只比 version+content_hash，状态变更（同版本）被当 no-op。"""
import tempfile, unittest
from pathlib import Path
from mimir_v8.operations import reproject_facts
from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.graph_projector import GraphProjector
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class TestReproject(unittest.TestCase):
    def test_status_change_reaches_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = CanonicalStore(root / "canonical.db")
            fts = FTSProjector(root / "fts.db"); graph = GraphProjector(store, root / "graph.db")
            out = store.create_fact(CreateFact(content="c", owner_principal="mentor", domain="system", fact_type="pattern", summary="s", idempotency_key="k1"), actor_principal="admin")
            fid = out["fact_id"]
            for p in (fts, graph):
                ProjectorRunner(store, p).run_once(limit=10)
            # simulate the historical drift: canonical flips to disputed without a version bump
            with store.transaction() as c:
                c.execute("UPDATE facts SET status='disputed' WHERE fact_id=?", (fid,))
            report = reproject_facts(store, [fts, graph], [fid, "missing-id"])
            self.assertEqual(report["reprojected"], [fid]); self.assertEqual(report["missing"], ["missing-id"])
            with fts.connect() as c:
                self.assertEqual(c.execute("SELECT status FROM projected_facts WHERE fact_id=?", (fid,)).fetchone()[0], "disputed")
                self.assertEqual(c.execute("SELECT COUNT(*) FROM facts_fts WHERE fact_id=?", (fid,)).fetchone()[0], 0)
            with graph.connect() as c:
                self.assertIsNone(c.execute("SELECT fact_id FROM fact_nodes WHERE fact_id=?", (fid,)).fetchone())
```
  If `store.create_fact` needs different kwargs (check `CreateFact` in `schema.py:250-270` — `source_kind/source_uri` may be required), adapt to the minimal valid set used in `tests/test_p22_collector_wiring.py`.
- [ ] **Step 2:** Run → FAIL (`reproject_facts` missing; after adding it, status stays `active` because of the guard).
- [ ] **Step 3:** `projector.py` guard: extend the no-op condition with `and existing["status"] == fact["status"]` (add `status` to the `SELECT version, content_hash` query). Then in `operations.py`:
```python
def reproject_facts(store: CanonicalStore, projectors: list, fact_ids: list[str]) -> dict:
    """Re-apply each fact's current canonical row to the given projectors.

    Repairs drift left by older projector code (2026-09-07 audit P1-1: three
    facts marked disputed on 08-22/08-25 stayed 'active' in fts/graph/vector).
    Uses each projector's own apply() with the fact's latest memory_event, so
    no projection logic is duplicated here and nothing is written to canonical.
    """
    report = {"reprojected": [], "missing": [], "skipped_no_event": []}
    for fact_id in fact_ids:
        try:
            fact = store.get_fact(fact_id)
        except Exception:
            report["missing"].append(fact_id); continue
        with contextlib.closing(store.connect()) as connection:
            event = connection.execute(
                "SELECT * FROM memory_events WHERE aggregate_type='fact' AND aggregate_id=? ORDER BY event_seq DESC LIMIT 1",
                (fact_id,)).fetchone()
        if event is None:
            report["skipped_no_event"].append(fact_id); continue
        for projector in projectors:
            projector.apply(event, fact)
        report["reprojected"].append(fact_id)
    return report
```
  `migrate_cli.py`: add subparser `reproject` with `--data-dir` (required), `--fact-ids` (comma list, required), `--with-vector` flag, `--collection` (default env `MIMIR_V8_COLLECTION`). Build `store = CanonicalStore(root/"canonical.db")`, projectors `[FTSProjector(root/"fts.db"), GraphProjector(store, root/"graph.db"), CoreMemoryProjector(store, root/"core_memory.db")]`; if `--with-vector`: `import chromadb; client = chromadb.PersistentClient(path=str(root/"chroma"), settings=Settings(anonymized_telemetry=False)); collection = client.get_collection(name)`; `VectorProjector(collection, embedder=_no_embed, collection_name=name)` where `def _no_embed(text): raise RuntimeError("reproject refuses to embed; only status/deletion repairs are supported offline")`. Print JSON report; exit 1 if `missing`.
- [ ] **Step 4:** Run `tests/test_p47_reproject.py tests/test_r*.py -k "projector or fts or graph"` (plus the full suite quickly minus r8) → pass.
- [ ] **Step 5:** `git add -A mimir_v8/projector.py mimir_v8/operations.py mimir_v8/migrate_cli.py tests/test_p47_reproject.py && git commit -m "fix(projection): status-aware FTS guard + reproject CLI to repair disputed-fact drift (audit P1-1)"`

### Task 7: Version 14.1.0, CHANGELOG, docs & packaging fixes

**Files:** `mimir_v8/schema.py:8`, `pyproject.toml:7,19-29`, `tests/test_r8_release.py:79`, `CHANGELOG.md` (CRLF!), `SECURITY.md:5-10`, `docs/ROADMAP.md:115`, `scripts/init.sh` (admin token + config template), `examples/QUICKSTART.md:20-48`, `Dockerfile:44` + `mimir_v8/server.py:65`, `hermes-plugin/mimir_memory_provider/plugin.yaml` (done in Task 4).

- [ ] **Step 1:** `schema.py` `MIMIR_VERSION = "14.1.0"`; `pyproject.toml` `version = "14.1.0"` and add `"pyyaml>=6.0"` to `dependencies`; `test_r8_release.py` `assertEqual(MIMIR_VERSION, "14.1.0")`.
- [ ] **Step 2:** `server.py`: replace the loopback check with
```python
    loopback = {"127.0.0.1", "::1", "localhost"}
    if args.bind not in loopback and os.environ.get("MIMIR_ALLOW_NONLOOPBACK") != "1":
        raise SystemExit("Mímir v9 server may only bind to loopback (set MIMIR_ALLOW_NONLOOPBACK=1 inside a container network namespace)")
```
  `Dockerfile`: add `MIMIR_ALLOW_NONLOOPBACK=1 \` to the ENV block and `ENTRYPOINT ["mimir-server", "--bind", "0.0.0.0", "--port", "8456"]`. Same ENTRYPOINT change in `Dockerfile.lean` if it has one.
- [ ] **Step 3:** `scripts/init.sh`: generate a dedicated `admin` principal — in the Python heredoc, after the agents loop, append an `admin` entry with `scopes ["read","write","delete","ingest","review","manage","admin"]`, `admin: True`, write `clients/admin.token`; set agents to `["read","write"]` only (drop the `i == 0` admin promotion). Replace the config heredoc with:
```yaml
# Mímir config — only these sections are read by the code (audit 2026-09-07):
#   federation.agents / federation.domains  → dynamic principal & domain registry (config.py)
#   collector.rss_feeds / collector.sources → unified ingestion registry (worker.py)
version: 14.1.0
federation:
  agents: [heimdallr, quantmaster, jarvis, mentor]
  domains: [infrastructure, quant, tech_support, personal, system, knowledge]
collector:
  rss_feeds: []
  sources: []
  # example:
  # - {name: vault, type: vault, vault_root: /home/me/obsidian, exclude_dirs: [private], category: knowledge_doc}
  # - {name: blog, type: web, url: https://example.com/post}
```
- [ ] **Step 4:** `examples/QUICKSTART.md`: exports become `export MIMIR_V8_DATA_DIR=~/.hermes/mimir/data` and `export MIMIR_V8_TOKEN_FILE=~/.hermes/mimir/secrets/api_tokens.json` (after `scripts/init.sh`); every curl gains `-H "Authorization: Bearer $(cat ~/.hermes/mimir/secrets/clients/admin.token)"`. `SECURITY.md` table → `| 14.1.x | 20 | ✅ |`, `| 12.1 – 14.0 | 19–20 | ⚠️ upgrade |`, `| < 12.1 | < 19 | ❌ |`. `docs/ROADMAP.md:115` `[ ] **P1 韧性挡位` → `[x]` with note "(shipped cd9cf41, v14.1.0)".
- [ ] **Step 5:** `CHANGELOG.md` (keep CRLF — edit with a Python script that reads bytes, inserts the block after the `---` following the header with `\r\n` endings): new section `## v14.1.0 — 2026-09-07 · 全面审计修复 (Audit Remediation)` listing: P0 governance env fallback + honest errors + requeue cap + worker exit codes; error_hook wired; human approvals → confirmed; plugin per-agent tokens/idempotency/reflect/errors; dashboard backend/ layout + v11 route + POST review + invalidate-all + random session secret + netErr banner; projection status guard + `reproject` CLI; init.sh admin token + config template; Docker bind/port; QUICKSTART/SECURITY/ROADMAP/README doc fixes; #41-A vault→wiki; golden-health sentinel; pyyaml declared.
- [ ] **Step 6:** Local run `python -m pytest tests -q -p no:cacheprovider --ignore=tests/test_r8_release.py -x --deselect tests/test_v12_insight.py::TestM1dHermesPluginContract` (indicative; platform debts are ~21 known failures — compare against the pre-existing list, only new failures matter). Commit: `git add -A && git commit -m "chore(release): v14.1.0 — audit remediation; docs/packaging/init fixes"`.

### Task 8: Authoritative regression on the device

- [ ] **Step 1:** `git push lan v14.1.0` (branch only). On device: `cd ~/mimir-open-source && git worktree add /tmp/mimir-v14.1.0 v14.1.0`.
- [ ] **Step 2:** `cd /tmp/mimir-v14.1.0 && PYTHONPATH=$PWD ~/.hermes/mimir/venvs/v14.0.0-20260903/bin/python3 -m pytest tests -q --no-header -p no:cacheprovider 2>&1 | tail -15` → expect `N passed, 3 errors` with N ≥ 444 + new tests, the 3 errors being `TestM1dHermesPluginContract` only. Fix anything else before proceeding.
- [ ] **Step 3:** `git worktree remove /tmp/mimir-v14.1.0` on device.

### Task 9: Release v14.1.0 and deploy

- [ ] **Step 1:** Local: `git checkout master && git merge --ff-only v14.1.0 && git push lan master` (device tree clean → updateInstead updates it). Device: `cd ~/mimir-open-source && git tag -a v14.1.0 -m "v14.1.0 audit remediation" && git push gitee master --tags && git push github master --tags` (remote names per `git remote -v` on device).
- [ ] **Step 2:** Release tree: `cd ~/.hermes/mimir/releases && git -C ~/mimir-open-source archive --format=tar --prefix=v14.1.0-20260907/ v14.1.0 | tar -xf -` → verify `grep MIMIR_VERSION v14.1.0-20260907/mimir_v8/schema.py`.
- [ ] **Step 3:** venv: `cp -a ~/.hermes/mimir/venvs/v14.0.0-20260903 ~/.hermes/mimir/venvs/v14.1.0-20260907`; `grep -rl "v14.0.0-20260903" ~/.hermes/mimir/venvs/v14.1.0-20260907/lib/python3.11/site-packages/__editable__* ~/.hermes/mimir/venvs/v14.1.0-20260907/lib/python3.11/site-packages/mimir_v8-*.dist-info/direct_url.json | xargs sed -i 's#v14.0.0-20260903#v14.1.0-20260907#g'`; verify `cd /tmp && ~/.hermes/mimir/venvs/v14.1.0-20260907/bin/python3 -c "import mimir_v8, mimir_v8.schema as s; print(mimir_v8.__file__, s.MIMIR_VERSION)"` → new path, 14.1.0.
- [ ] **Step 4:** Units: `mkdir -p ~/.hermes/mimir/backups/units-pre-v14.1.0-20260907 && cp /etc/systemd/system/mimir*.service ~/.hermes/mimir/backups/units-pre-v14.1.0-20260907/`; `sudo sed -i 's#v14.0.0-20260903#v14.1.0-20260907#g' /etc/systemd/system/mimir*.service`; `sudo sed -i '/^ExecStart=/i EnvironmentFile=/home/sandro1123/.hermes/mimir/secrets/evaluator.env' /etc/systemd/system/mimir-v9.2-governance.service`; `sudo systemctl daemon-reload`; `grep -c v14.1.0 /etc/systemd/system/mimir*.service` all ≥1; `systemctl cat mimir-v9.2-governance.service | grep EnvironmentFile`.
- [ ] **Step 5:** ops scripts: `cd ~/.hermes/mimir/ops && for f in mimir_v8_ops.py mimir_v8_cron_wrapper.py mimir_outlet_wrapper.py daily_report_feishu.sh; do cp $f $f.bak-pre-v14.1.0-20260907; sed -i 's#v14.0.0-20260903#v14.1.0-20260907#g' $f; done`; `ln -sfn v14.1.0-20260907 ~/.hermes/mimir/venvs/current`.
- [ ] **Step 6:** Repair + restart: `sudo systemctl stop mimir.service`; `cd ~/.hermes/mimir/releases/v14.1.0-20260907 && PYTHONPATH=$PWD ~/.hermes/mimir/venvs/v14.1.0-20260907/bin/python3 -m mimir_v8.migrate_cli reproject --data-dir ~/.hermes/mimir/v9/production-v9.0-20260805_214614 --fact-ids 7bac0264-68e5-46d7-be08-f63a6f02dd36,104a19d1-<full>,1647cde7-<full> --with-vector --collection mimir_v9_prod_20260805_214614` (full ids from `SELECT fact_id FROM facts WHERE status='disputed'`); `sudo systemctl start mimir.service`; `sleep 30; curl -s 127.0.0.1:8456/health` → `14.1.0`; `/ready` 200.
- [ ] **Step 7:** Verify drift gone: `~/.hermes/mimir/venvs/current/bin/python3 ~/.hermes/mimir/ops/mimir_v8_ops.py verify` → `"ok": true`.
- [ ] **Step 8:** Governance live check: `sudo systemctl start mimir-v9.2-governance.service && journalctl -u mimir-v9.2-governance.service -n 3 -o cat | tail -c 600` → stats no longer all `human_review`; `sqlite3`-via-python: `SELECT success, model, COUNT(*) FROM candidate_review_assessments WHERE created_at >= <now-10min> GROUP BY 1,2` shows `success=1` rows with model set. `systemctl show -p Result mimir-v9.2-governance.service` = success.
- [ ] **Step 9:** Dashboard: `cp ~/mimir-dashboard/backend/main.py ~/mimir-dashboard/backend/main.py.bak-v14.1.0-20260907; cp ~/mimir-dashboard/frontend/index.html ~/mimir-dashboard/frontend/index.html.bak-v14.1.0-20260907; cp ~/mimir-open-source/dashboard/backend/main.py ~/mimir-dashboard/backend/main.py; cp ~/mimir-open-source/dashboard/frontend/index.html ~/mimir-dashboard/frontend/index.html; sudo systemctl restart mimir-dashboard`; `curl -s -o /tmp/l.html http://127.0.0.1:8800/ && md5sum /tmp/l.html ~/mimir-open-source/dashboard/frontend/index.html` equal; `/api/dashboard/health-light` 200 with token.
- [ ] **Step 10:** Plugin: `cp -a ~/.hermes/plugins/mimir_memory_provider ~/.hermes/plugins/mimir_memory_provider.bak-v14.1.0-20260907; cp ~/mimir-open-source/hermes-plugin/mimir_memory_provider/{__init__.py,tools.py,plugin.yaml} ~/.hermes/plugins/mimir_memory_provider/`; `python3 -c "import ast;ast.parse(open('/home/sandro1123/.hermes/plugins/mimir_memory_provider/tools.py').read())"`. Do NOT restart gateways.
- [ ] **Step 11:** Production repo: `cd ~/.hermes/mimir && git rm --cached collect/rss_seen_urls.json && echo 'collect/rss_seen_urls.json' >> .gitignore && git add -A ops .gitignore && git commit -m "deploy: v14.1.0-20260907 — units/ops re-pointed, governance EnvironmentFile, runtime state untracked"`.

### Task 10: Ops health checks (production repo)

**Files:** `~/.hermes/mimir/ops/mimir_v8_ops.py` `cmd_health` pipeline block (after `review_backlog`).

- [ ] **Step 1:** Insert (inside the same `try:` using `cur`):
```python
        # 治理 LLM 成功率（审计 2026-09-07 P0-1：零成功持续 6 天而 health 恒 ok）
        since_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        gov = cur.execute(
            "SELECT COALESCE(SUM(success=1),0) AS ok, COUNT(*) AS n FROM candidate_review_assessments WHERE created_at >= ?",
            (since_2h,)).fetchone()
        report["governance_assessments_2h"] = {"total": gov["n"], "ok": gov["ok"]}
        if gov["n"] >= 3 and gov["ok"] == 0:
            failures.append(f"governance LLM: 0/{gov['n']} successful assessments in last 2h")
        hr = cur.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM candidate_facts WHERE status='human_review'").fetchone()
        report["human_review_backlog"] = hr["n"]
        if hr["n"] > 40:
            failures.append(f"human_review backlog high: {hr['n']}")
        if hr["oldest"]:
            age_d = (datetime.now(timezone.utc) - datetime.fromisoformat(hr["oldest"])).total_seconds() / 86400
            report["human_review_oldest_days"] = round(age_d, 1)
            if age_d > 14:
                failures.append(f"human_review oldest candidate {round(age_d,1)}d unreviewed")
        marker = HOME / ".hermes/mimir/backups/.last-offsite-success"
        if marker.exists():
            age_h = (time.time() - marker.stat().st_mtime) / 3600
            report["offsite_backup_age_hours"] = round(age_h, 1)
            if age_h > 48:
                failures.append(f"offsite backup stale: {round(age_h,1)}h")
        else:
            report["offsite_backup_age_hours"] = None
```
  (add `timedelta` to the datetime import.)
- [ ] **Step 2:** Run `~/.hermes/mimir/venvs/current/bin/python3 ~/.hermes/mimir/ops/mimir_v8_ops.py health --allow-lag` → `ok: true`, report has the new keys. Commit in prod repo.

### Task 11: Mímir offsite backup (production)

**Files:** Create `~/.hermes/mimir/ops/mimir_offsite_backup.sh`; create `~/.config/systemd/user/mimir-offsite-backup.{service,timer}`; modify laptop `C:\Users\sandr\hermes-offsite\prune.ps1` to also keep 7 `mimir-critical-*`.

- [ ] **Step 1:** Script (same recipient/key/hosts as `~/.hermes/bin/offsite_backup.sh`): stage newest `~/.hermes/mimir/v8/backups/canonical-*.db` (gzip), `secrets/` (0600 preserved), `mimir_config.yaml`, `ops/*.py *.sh` (no .bak), `/etc/systemd/system/mimir*.service|timer` (sudo -n cp), `core_memory/*.json`, MANIFEST.txt; `tar -czf - | gpg --encrypt --recipient hermes-backup@n100.local` → `/tmp/mimir-critical-<stamp>.tar.gz.gpg`; size gate ≥ 1MB; scp to `sandr@192.168.5.11:hermes-offsite/` (fallback 100.124.229.77); on success `touch ~/.hermes/mimir/backups/.last-offsite-success`; then `ssh ... powershell -NoProfile -File C:\Users\sandr\hermes-offsite\prune.ps1`.
- [ ] **Step 2:** `prune.ps1`: add a second block for `mimir-critical-*.tar.gz.gpg` with `$KeepMimir = 7`, output `PRUNE_OK kept=… mimir_kept=…`.
- [ ] **Step 3:** Timer `OnCalendar=*-*-* 03:50:00`, `Persistent=true`; `systemctl --user daemon-reload && systemctl --user enable --now mimir-offsite-backup.timer`; run once: `systemctl --user start mimir-offsite-backup.service; journalctl --user -u mimir-offsite-backup.service -n 5 -o cat` → `OK via=…`; laptop `ls C:\Users\sandr\hermes-offsite\mimir-critical-*` exists; marker file exists. Commit script in prod repo.

### Task 12: Backfill human-approved facts to `confirmed` (via API, event-sourced)

- [ ] **Step 1:** Dry-run count on device (ro): `SELECT COUNT(*) FROM facts f JOIN candidate_facts c ON c.committed_fact_id=f.fact_id WHERE f.human_status='unreviewed' AND c.status='committed' AND c.reviewed_by IS NOT NULL AND c.reviewed_by NOT LIKE 'service:%'` → expect ≈128.
- [ ] **Step 2:** Script: for each fact_id `PATCH /v8/facts/{id}` with `{"human_status": "confirmed"}` using `admin.token`; count 200s; re-run the SELECT → 0. Sample one fact's events → `fact.updated` with `human_status` change recorded.

### Task 13: Device hygiene (P3)

- [ ] `sudo rm /etc/systemd/system/multi-user.target.wants/mimir-v8.1-staging.service && sudo systemctl daemon-reload` → `systemctl list-units --all | grep staging` empty.
- [ ] `sudo mkdir -p ~/.hermes/mimir/backups/units-bak-archive-20260907 && sudo mv /etc/systemd/system/mimir*.bak-* ~/.hermes/mimir/backups/units-bak-archive-20260907/ && sudo mv /etc/systemd/system/mimir-v9.2-daily-report.* /etc/systemd/system/mimir-weekly-reflect.* ~/.hermes/mimir/backups/units-bak-archive-20260907/ && sudo chown -R sandro1123: ~/.hermes/mimir/backups/units-bak-archive-20260907 && sudo systemctl daemon-reload`.
- [ ] `rm -rf ~/mimir-open-source/tmp_sync` (both files verified identical to HEAD).
- [ ] `mkdir -p ~/.hermes/mimir/archive/decoys-20260907 && mv ~/.hermes/mimir/mimir.db ~/.hermes/mimir/data/canonical.db* ~/.hermes/mimir/core/fts5_index.db ~/.hermes/mimir/archive/decoys-20260907/` and write `~/.hermes/mimir/archive/decoys-20260907/README.txt` explaining the true DB path.
- [ ] `mkdir -p ~/.hermes/mimir/backups/ops-bak-archive-20260907 && mv ~/.hermes/mimir/ops/*.bak-* ~/.hermes/mimir/backups/ops-bak-archive-20260907/` (after Task 9 step 5 created new .bak files — keep those new ones: move only files not matching `*pre-v14.1.0*`).
- [ ] `rm ~/mimir-dashboard/dashboard.pid; mv ~/mimir-dashboard/dashboard.log ~/mimir-dashboard/dashboard.log.pre-systemd-20260904` and archive the 19 `*.bak-*` files in `~/mimir-dashboard/{backend,frontend}` into `~/mimir-dashboard/bak-archive-20260907/` (keep the v14.1.0 ones).

### Task 14: Final verification and records

- [ ] `health --allow-lag` ok with new keys; `verify` ok; `/health` 14.1.0; `/ready` 200; dashboard md5 match; governance unit `Result=success` and `success=1` assessments present; `systemctl --failed` empty; offsite marker fresh; `git status` clean in both repos; local `git log --oneline -12`.
- [ ] Update memory file `mimir-full-audit-20260907.md` with a "修复收官" section (commits, release dir, what remains: gateway restart pending, P2 items deferred) and the MEMORY.md hook; append a §9 "处置结果" to the audit report (local + vault).
