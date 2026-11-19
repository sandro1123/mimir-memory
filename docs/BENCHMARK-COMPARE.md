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

## Extensibility registries (1.8.0 spec: agent adapters + memory sources)

**Status:** implemented (1.8.0 dev, `feat/1.8.0`) — spec `docs/superpowers/specs/2026-04-11-mimir-1.8-agent-adapter-and-memory-source-extensibility-design.md` §9 Tasks 1–5.

**Implemented (evidence):**
- **Adapter contract & registry** (`mimir_v8/adapters/registry.py`): `AdapterSpec` (identity/tier/capabilities/endpoint bindings/connector & idempotency conventions), `validate_adapter_spec` (fail closed: kebab-case id, A-tier requires hook capability, hook paths pinned to `/v8/hooks/session-start|session-end`, `explicit` forbidden as default mode), in-process registry (`register_adapter/get_adapter/require_adapter/list_adapters`) with builtins `claude-code` + `codex` migrated from the pre-registry `HOOKS_A_TIER` table. Non-sensitive `manifest(spec)` for introspection/docs/1.9 installer.
- **Compat facade** (`mimir_v8/adapters/hooks_a_tier.py`): `HOOKS_A_TIER` and `build_session_end_payload(...)` keep pre-registry behavior (registry now backs the table; `adapter_id=` applies registry retention defaults).
- **Source connector contract & registry** (`mimir_v8/sources/registry.py`): `SourceSpec` (category/ingest_kind/interaction modes/allowed memory modes/extraction policy/idempotency), `validate_source_spec` (fail closed: conversation sources can never allow `explicit`; `auto_candidate` requires candidate/review policy; modes default must be allowed), builtins matching `classifier.SOURCE_CATEGORY_MAP` exactly (10 connectors). Quarantine fallback policy for unknown types (`auto_candidate=False`, no `observe`).
- **Classifier facade** (`mimir_v8/classifier.py`): `classify` delegates to the registry; static `SOURCE_CATEGORY_MAP`/type sets preserved as compat constants. Parity pinned by `tests/test_source_registry.py`.
- **Policy wiring** (`mimir_v8/learning.py`): `LearningService._check_source_policy` consults registry policy before storage/crystallization — registered connectors reject disallowed `memory_mode`; session_end auto-crystallization requires `auto_candidate=True` (unknown/quarantined types can never auto-promote; baseline quarantine-storage behavior preserved).
- **Declarative manifests** (`mimir_v8/adapters/manifests.py`): strict YAML/JSON loader for `MIMIR_ADAPTERS_DIR` / `MIMIR_SOURCES_DIR` (or explicit dir); all-or-nothing batch registration; unset = no-op (dynamic plugins/entry-points deliberately off, spec §8).
- **Additive introspection** (`mimir_v8/api.py`): `GET /v8/hooks/adapters` and `GET /v8/sources/connectors` (read-scoped, non-sensitive manifests/policy summaries).
- **Exemplars**: `tests/test_manifest_loading.py` loads an `opencode` B-tier adapter manifest and an `odin_auto` Thor/Odin auto-collection source manifest through the governed path (registration → classification → policy), proving spec §5.6's first onboarding target works by registration only.

**Verified by tests:**
- `tests/test_adapter_registry.py` (builtin registration, capability/tier filters, override semantics, fail-closed validation incl. explicit-mode and nonstandard-path rejection, compat facade behavior unchanged, manifest shape)
- `tests/test_source_registry.py` (classifier parity for all 10 builtins, quarantine fallback, policy defaults per category, registration contract, Thor/Odin exemplar registration)
- `tests/test_manifest_loading.py` (valid load, invalid aborts whole batch, unset env no-op, explicit rejected, conversation sources cannot allow explicit)
- `tests/test_introspection_api.py` (both endpoints: read scope enforced, builtin listing, no secrets)
- Regression: full suite green (no behavior drift for pre-registry callers; `mex` import heat-column defaults; `p44` rebuild preserves heat columns)

**Explicitly pending / out of scope (1.9+):**
- `mimir-mcp`/CLI adapter installer and per-client config generation (roadmap §6/§12)
- B/C-tier runtime hook wiring; PostToolUse trajectory pipeline; pull schedulers/CDC workers for new sources
- Entry-point plugin loading (contract defined, disabled by default)

**Evidence commands (run in `~/mimir-1.8.0-dev`):**
```bash
python3 -m pytest tests/test_adapter_registry.py tests/test_source_registry.py tests/test_manifest_loading.py tests/test_introspection_api.py -q
python3 -m ruff check mimir_v8 tests
```
