# Mímir Architecture — v12.0

> 同步版本：v12.0.0 · Schema 18 · 代号 Insight · 2026-08-14

---

## 1. Storage (SQLite canonical)

**Core anchor**: `facts` (25 columns) with `'active'|'tombstoned'|'disputed'|'archived'` statuses; event sourcing with `memory_events`append-only, trigger-protected.

**Projection fanout**: `outbox` → vector / FTS / graph / core_memory.

### Immutability and Audit

```
memory_events:
  CREATE TRIGGER memory_events_no_update BEFORE UPDATE ON memory_events BEGIN RAISE(ABORT, 'memory_events are immutable'); END;
  CREATE TRIGGER memory_events_no_delete BEFORE DELETE ON memory_events BEGIN RAISE(ABORT, 'memory_events are immutable'); END;
fact_versions: same approach.
Audit trail from audit_log with request_id/category reference.
```

### New v10 tables (schema 13)

```
opinions(opinion_id, fact_id, topic, stance('support'|'oppose'|'neutral'),
  confidence REAL, evidence_ids TEXT, owner_principal, created_at, updated_at)

observations(observation_id, summary(topic), supporting_opinion_ids TEXT,
  confidence REAL, stale 0/1, owner_principal, created_at, updated_at)
```

Supporting CLI/commands read these via `mimir_v8.worker consolidate` (single-run accumulate≥3 same-topic opinions → create observation).  `run_governance_once` handles review queue under scope.

---

## 2. API Layer (FastAPI)

| Area | Endpoints | Scope |
|----|---|---|
| 发现 `/v8/query` | POST | `read` |
| 事实 CRUD `/v8/facts*` | POST/PATCH/DELETE | `write`/`delete`/`manage` |
| Learning pipeline `/v8/learning/*` | GET/POST | `read`/`write` |
| Candidate workflow `/v8/candidates*` | POST | `ingest`/`review` |
| v9 Knowledge `/v9/knowledge/*` | GET/POST | `read`/`write` |
| v10 Governance `/v10/opinions` | GET/POST | GET: read, POST: write |
| `/v10/observations` | GET | `read` |
| `/v10/opinions/consolidate` | POST | `manage` |
| `/v10/governance/run` | POST | `manage` |
| `/v10/candidates/{id}/fast_track` | POST | `write` |

Headers: `Authorization: Bearer <token>`; scope from `TokenStore``. HTTP 401 (missing/invalid token) / 403 (insufficient scope).

---

## 3. Query (Document Relevance Metering)

```
QueryRequest → RelevanceGate#should_search
  (heuristic skip: empty / greetings / short chat /记忆关键词/实体/疑问句)
  → Vector SEARCH (Chroma, cosine nRank 50)
  → FTS SEARCH (SQLite FTS5)
  → Graph LOOKOUT (graph edges neighborhood expansion)
  → RRF merge (k=60) → ACL filter → final decay and trust reweight
  → Top-K + `score_explanation` for auditability
```

**v10 improvement**: `include_provisional` flag includes `status='provisional' results`, otherwise dropout for `fast_tracked` thoughts.

---

## 4. Governance pipeline (v10, within package)

`worker governance` (systemd timer每五分钟）
```
:: review_required ordered created_at
→ LLM assessment (deepseek-v4-flash via 9router, temperature 0.1, max_tokens 512)
→ make_decision:
    is_noise → auto_reject (√ no fast, no fast follow)
    risk high/critical → human_review
    risk low & confidence≥0.7 → provisional
    else human_review
→ review_candidate(action=approve|reject)	update stats → commit_approved (atomic, outbox fan-out)
```

**fast_track** endoint manually promotes a pending candidate after human confidence was shown (0.5), otherwise the systemopoldance sends human_review back up.

---

## 5. Opinion/Observation Layer (v10)

**Opinions**:
- **写入/更新**: agent set_opinion({"fact_id","topic","stance","confidence","owner"}),`UNIQUE(fact_id, owner_principal)`汽笛量
- **演化**: signal `confirm/useful` → confidence ±0.1
- **合并**: `mimir_v8.worker consolidate` —≥3 same-topic opinions form `observations` (stale=0)

**observations**: summaries built on top of stronger opinions (confidence ≥ 0.6),当 queried when many open for deduction at will.

---

## 6. Dashboard

`~/mimir-dashboard`
- ASP.NET style backend FastAPI (8800) reverse proxy to Mímir HTTP endpoints
- Frontend single index.html (Alpine.js + Chart.js), **9 tabs**: overview / memory / review / sources / agents / opinions / system / symbolic / codegraph
- **v11 improvement**: Claude-style redesign (warm cream/charcoal theme, serif headings, coral accent, dark/light toggle, safe-area mobile bottom nav); fixed opinions tab escaped-quote rendering bug; fixed `/api/source/add` duplicate function; added v11 proxy routes (`/v11/symbolic/*`, `/v11/code/*`).

---

## 7. v11 Symbolic Memory + CodeGraph (new)

### Symbolic short-term memory (`mimir_v8/symbolic_memory.py`)
Inspired by TencentDB Agent Memory:
```
Verbose tool logs → offload to symbolic_blocks (node_id, summary, raw_text)
  → Mermaid canvas (symbolic_canvases) in context
  → Agent reasons over canvas → drill-down via node_id → full raw text
```
- `symbolic_blocks` / `symbolic_canvases` tables (schema 14)
- API: `/v11/symbolic/offload`, `/v11/symbolic/canvas`, `/v11/symbolic/{node_id}`

### CodeGraph (`code_symbols` + `code_relations`)
- Index code symbols (name/kind/file/line/signature/doc)
- Record callers/callees edges
- API: `/v11/code/search`, `/v11/code/impact/{symbol_id}`

### v10 reflect/federation (replaced placeholders)
- `/v10/reflect/{topic}` — synthesize insight from related facts + opinions
- `/v10/federation/{peer_hierarchy:path}` — cross-principal shared search with ACL

---

## 8. Timer cron jobs (systemd)

```
mimir-v9.2-cdc every 5min (via worker hermes-cdc)
mimir-v9.2-governance every 15min
mimir-v9.2-review-reminder daily
mimir-v9.2-daily-report daily
mimir-v9.2-decay-scan every 24h
mimir-v9.2-collect-all every 30min
mimir-v9.2-trust-update every hour
```

## 9. v12 Insight (schema 15→18)

```
schema 15  EvolveMem: search_feedback, quality_metrics; facts.valid_from/valid_to (Chronos)
schema 16  Conflict: conflict_resolutions (detect/resolve -> loser disputed, never deleted)
schema 17  Crystal: crystal_candidates (7d topic cluster >=3 -> candidate -> human approve)
schema 18  Multi-modal: fact_assets (image/audio/document/file refs, publish-policy guarded)
```

- **Retrieval funnel** — `QueryKernel.trace()`: RelevanceGate → CandidatePool → JaccardDedup → ChronosDecay → TopK (`POST /v12/search/trace`).
- **EvolveMem** — `/v12/evolve/*`; systemd `mimir-v12-evolve` every 6h aggregates 7d feedback, nudges confidence ±0.05, audit events only.
- **Conflict** — `/v12/conflicts/*`; losers flip `status='disputed'` + `fact.conflict_lost` event (history preserved).
- **Crystallization** — `/v12/crystals/*`; systemd `mimir-v12-crystallize` daily 00:30; approve materializes a pattern fact via `create_fact(connection=...)`.
- **Multi-modal** — `/v12/facts/{id}/assets`; assets render as Obsidian `![[embed]]` in `wikilink.py` notes (deterministic slugs + Backlinks double-linking).
- **MCP** — 27 tools (Core CRUD / Facts / Reflect / Evolve / Conflict / Crystal / Trace / Multi-modal) backed by `client.MimirAPIClient` over the v12 REST surface.
- **Packaging** — PyPI wheel+sdist via `pyproject.toml` (`mimir-server/worker/migrate/cli` entrypoints); self-contained `Dockerfile` (ghcr pull), dashboard ships separately from `mimir-dashboard/`.