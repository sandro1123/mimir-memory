# Mímir — Federated Memory System

> Built for persistent, governed, queryable memory of AI assistants

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-18-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

---

## What Mímir Is

Mímir is a **federated, event-sourced memory system** designed for AI agents and knowledge workers. Unlike vector databases that treat memory as embeddings or RAG pipelines that discard evolution signals, Mímir treats memories as **immutable events** with explicit lifecycle, governance, and multi-layer queries.

### Core Principles

- **Event-Sourced**: every fact is an append-only event stream with ACID triggers
- **Governed**: LLM-assisted classification, human review, audit log, ACL per fact
- **Queryable**: vector + full-text + graph channels fused into ranked results with RRF
- **Durable**: SQLite canonical store with immutability triggers for `memory_events` and `fact_versions`

---

## Quick Start

### Environment

```bash
# clone
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory

# install deps
pip install fastapi uvicorn httpx jinja2 aiofiles chromadb sentence-transformers

# environment (create your own secrets under MIMIR_HOME/secrets)
export MIMIR_HOME=~/.hermes/mimir
export MIMIR_DATA_DIR=$MIMIR_HOME/data
export MIMIR_SECRETS_DIR=$MIMIR_HOME/secrets
export MIMIR_CONFIG_FILE=$MIMIR_HOME/mimir_config.yaml

# serve loopback-only
python -m mimir_v8.server --bind 127.0.0.1 --port 8456
```

### API Quick Reference

| Endpoint | Method | Auth | What it does |
|----------|--------|------|-------------|
| `/health` | GET | none | Liveness |
| `/ready` | GET | none | readiness + projector lag |
| `/v8/query` | POST | read | ranked facts by vector/fts/graph RRF |
| `/v8/learning/remember` | POST | write | candidate → governance pipeline |
| `/v8/learning/candidates` | GET | review | pending candidates |
| `/v8/learning/candidates/{id}/review` | POST | review | approve / reject |
| `/v8/learning/status` | GET | read | learning pipeline status |
| `/v10/opinions` | GET/POST | write | subjective stances, confidence evolution |
| `/v10/observations` | GET | read | consolidated summaries from opinions |
| `/v10/governance/run` | POST | manage | run LLM governance pipeline |
| `/v10/opinions/consolidate` | POST | manage | auto-generate observations (≥3) |
| `/v10/candidates/{id}/fast_track` | POST | write | auto approve & commit bypass |

---

## Architecture

### Storage Layers (5 fan-outs per write)

```
facts (canonical)    ← single insert, core event source
   ↓
memory_events        ← append-only, trigger-immute
   ↓
fact_versions        ← immutable historical snapshot, ACID trigger
   ↓
four db files:   chroma (vector), fts.db (fts5), graph.db (graph), core_memory.db
                   ↑ outbox pattern with commit-following guarantees
```

### Governance Pipeline

```
any write memory(candidate, review_required)
       ↓
LLM governance assesses (risk & value) via a configurable local/remote LLM
  ├─→ auto_rejected (deterministic + LLM noise)
  ├─→ provisional (AI evaluates but unsure)
  ├─→ human_review (missing approval)
  ↓manual审批 → approved → commit → facts (status=active)
```

### Query Flow

```
Loader QueryKernel.search：
1. RelevanceGate (1ms heuristic——skip irrelevant chats)
2. Vector (chromadb cosmlkem)
3. FTS (SQLite FTS5)
4. Graph (one-hop neighbourhood expander)
→ RRF merge → ACL filter → decay×trust → top-K return, include_provisional flag supported
```

---

## Packages

### v10 release structure

```
releases/v10.0.0-20260811_104554/
├── mimir_v8/           # core package (~11k lines)
│   ├── api.py           # FAST API DAG (~1000 lines)
│   ├── governance.py    # v10 新：governance P〇〇(auto_log / fast track)
│   ├── opinion.py       # NEW: opinion confidence evolution, observations
│   ├── schema.py        # constants (version=10.0.0 schema=13)
│   ├── store.py         # canonical store + 29 tables + ACID + trigger + outbox
│   ├── worker.py        # systemd timer entry:all worker commands
│   └── ...
├── tests/               # R2-R8 regression (113 test defs)
├── README.md            # this file
├── ARCHITECTURE.md      # full architecture
├── CHANGELOG.md         # releases history
└── UPGRADE-ROADMAP-20260807.md  # future views

dashboard (separate. routes to v10 API 8800)
├── Dockerfile
├── docker-compose.yml
├── manage.sh            # local start/stop/lifecycle scripts
├── backend/main.py      # 15 existing endpoints + 5 new v10 routers
└── frontend/index.html  # Alpine.js SPA，7 tabs + 手机底导航
```

---

## Security

- **Loopback only**: API binds `127.0.0.1` only; external visibility via nginx/cloudflare tunnel
- **Bearer token auth + scope** in every endpoint (`read/write/review/manage/admin`)
- **SQLite triggers** prevent `memory_events` and `fact_versions` from UPDATE/DELETE
- All mutations carry **idempotency key** + actor_principal + audit logging
- Sensitive profiles can set `egress_policy=local_only` which blocks external processor upstream
- DLP regex redaction applied on ingestion and extraction

---

## Roadmap

| Milestone | Scope | Status |
|-----------|-------|--------|
| v10.0 | Governance main → in-package, Opinion/Observation layer, dashboard fix | ✅ shipped |
| v11.0 | Symbolic short-term memory + CodeGraph + reflect/federation API (inspired by TencentDB Agent Memory) | ✅ shipped |
| v12.0 (schema 18) | Insight: Ebbinghaus decay, Chronos, EvolveMem, recall funnel trace, conflict resolution, skill crystallization, MCP 27 tools, multimodal assets, PyPI/Docker packaging, Obsidian wikilink | ✅ shipped |
| v12+ | Hermes MemoryProvider live integration, retrieval eval baseline | 🔵 in progress |

See `UPGRADE-ROADMAP-20260807.md` in the release dir for detail.

---

## License

[MIT](LICENSE)

### Inspiration

The v11.0 symbolic short-term memory and CodeGraph modules were inspired by [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) (MIT) — specifically its symbolic short-term memory (Mermaid canvas + offload/drill-down) and code graph indexing concepts.