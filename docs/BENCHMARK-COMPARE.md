# Mímir Benchmark / Parity Compare

Living evidence log for comparing Mímir behavior against target workflows and adjacent memory systems. Rows should cite implemented routes, invariants, and tests rather than aspirational claims.

## claude-mem (#34, A-tier hooks / STM bootstrap)

**Status:** partial (1.8.0 dev, `feat/1.8.0`)

**Implemented (evidence):**
- `GET /v8/hooks/session-start` (`mimir_v8/api.py`): read-scoped bootstrap payload via `_bootstrap_payload(identity)` (same identity-scoped shape as `/v15/bootstrap`), returns `principal_id`, `iron_rules`, `user_prefs`, `domain_counts` without leaking other principals' owner-only facts.
- `POST /v8/hooks/session-end` (`mimir_v8/api.py`, `mimir_v8/learning.py:LearningService.session_end`): ingest + auto-crystallize **one STM candidate** (`candidate_facts`) from the redacted conversation source; **no direct `facts` writes**. Rejects `memory_mode="explicit"` (use `remember()` path). DLP redaction applied before candidate persistence; idempotent replay returns same `run_id/source_id/candidate_id`.
- A-tier adapter scaffolding: `mimir_v8/adapters/hooks_a_tier.py` declares `HOOKS_A_TIER` start/end paths for `claude-code` and `codex` and `build_session_end_payload(...)` (ConversationEnvelope-compatible, rejects `memory_mode="explicit"`).

**Verified by tests:**
- `tests/test_hooks_session_start.py` (bootstrap payload, identity visibility, read-scope requirement)
- `tests/test_p34_hooks_session_end.py` (ingest+candidate, idempotent replay same candidate, explicit rejected, DLP redaction, HTTP wiring)
- `tests/test_hooks_integration.py` (start→end pipeline, idempotent end, start hides other-principal data)
- `tests/test_hooks_adapters.py` (adapter path declarations, payload shape, explicit rejected)

**Explicitly pending / out of scope for this slice:**
- PostToolUse trajectory capture
- B/C-tier adapters and richer harness integrations beyond declarative A-tier scaffolding
- Dashboard v5 / backend API contract changes (not part of 1.8 hooks slice)

**Evidence commands (run in `~/mimir-1.8.0-dev`):**
```bash
grep -n "hooks/session-start\|hooks/session-end" mimir_v8/api.py
python3 -m pytest tests/test_hooks_adapters.py tests/test_p34_hooks_session_end.py tests/test_hooks_session_start.py tests/test_hooks_integration.py -q
```
