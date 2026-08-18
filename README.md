# Mímir — The Memory That Remembers *How* to Forget

> An event-sourced, self-evolving, federated memory system for AI agents.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-18-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README.md) · [中文](README_zh.md)

---

## Why Mímir Is Different

Most memory systems are **databases with a nicer API** — they store vectors and
return the closest match. Mímir is built on a different premise: **a memory is
an event, not a row.**

That single decision changes everything downstream:

| A normal memory store | Mímir |
|---|---|
| Overwrites old memories | **Appends immutable events** — every change is a new event, history is never rewritten |
| "Forgetting" = deleting rows | **Tombstone forgetting** — forget without deleting; the fact is marked, not destroyed |
| Memory quality = your prompt | **Governed** — an LLM *evaluates* every candidate before commit; the LLM can only *suggest*, never *commit* |
| Static retrieval score | **Self-evolving** — search feedback (useful/useless/correction) nudges confidence over time |
| Single vector index | **Three-channel fusion** — vector + full-text + graph, fused by RRF with a local reranker |
| Facts decay arbitrarily | **Ebbinghaus decay** — five forgetting curves, from never-forget to ephemeral |

Mímir is not "another RAG layer." It is a **complete lifecycle** for agent memory:
*ingest → govern → commit → retrieve → self-correct → forget* — with every step
auditable and reversible.

---

## The Core Architecture in One Picture

```
                         ┌─────────────────────────────┐
   conversation          │         GOVERNANCE          │
   / ingestion ────────▶ │  candidate → LLM assess →   │
                         │  noise / provisional /       │
                         │  human-review / commit       │
                         └──────────────┬──────────────┘
                                        ▼
                         ┌─────────────────────────────┐
                         │   CANONICAL (event-sourced) │
                         │   facts + memory_events +    │
                         │   fact_versions (immutable)  │
                         └──────────────┬──────────────┘
                                        ▼  (outbox fan-out)
              ┌──────────────┬──────────┴──────────┬──────────────┐
              ▼              ▼                     ▼              ▼
          vector (chroma)  fts (FTS5)          graph           core_memory
              └──────────────┴──────────┬──────────┴──────────────┘
                                        ▼
                              RRF fusion + local rerank
                                        ▼
                              ranked, ACL-filtered results
```

---

## What Sets Mímir Apart — The Six Pillars

### 1. Event-Sourced Truth (不可篡改的账本)
Every fact is an append-only event stream. `memory_events` and `fact_versions`
are protected by SQLite triggers that **refuse UPDATE and DELETE**. You can
rewind, audit, and explain *why* a memory is what it is — as a structural
property, not a promise.

### 2. Governed Ingestion (治理闭环)
Before any candidate becomes a fact, it passes through a governance pipeline:
a deterministic rule engine plus an independent LLM assessor classify it as
noise, low-risk, or uncertain. The LLM is **deliberately separated** from the
commit path — the same model cannot extract *and* approve. Every decision lands
in `audit_log`.

### 3. Symmetric Self-Evolution (检索自进化)
Search feedback (`useful` / `useless` / `correction`) is aggregated over a
7-day window and nudges fact confidence — up *and* down, gated by a minimum
signal count so two lucky hits can't inflate a fact's weight. Memories get
*more* trustworthy the more they're used, and *less* trustworthy when they
misfire.

### 4. Scientific Forgetting (科学的遗忘)
Five decay tiers modeled on the Ebbinghaus curve, plus a Chronos dual-timeline
(`valid_from` / `valid_to`): identity-level rules never decay, ephemeral facts
half-life in 7 days, and expired facts are deweighted — **never deleted**.

### 5. Three-Layer Knowledge (三层知识)
Memory isn't one flat pile. Mímir separates **memory** (facts), **learning**
(methods/experience), and **wiki** (documents), each with its own lifecycle,
authorization, and feedback loop. Skill crystallization auto-clusters recurring
topics into reusable pattern facts — with a human in the loop.

### 6. Federated & Private by Default (联邦隔离 + 本地隐私)
Multi-agent isolation via `owner_principal` + ACL. All embeddings (bge-m3) and
reranking (ms-marco) run **locally on CPU** — text being embedded never leaves
your machine. API binds to `127.0.0.1` only.

---

## Quick Start

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory
pip install -e ".[embeddings]"        # includes local bge-m3 embedding

# create your own secrets under MIMIR_HOME/secrets
export MIMIR_HOME=~/.hermes/mimir
export MIMIR_DATA_DIR=$MIMIR_HOME/data
export MIMIR_SECRETS_DIR=$MIMIR_HOME/secrets

python -m mimir_v8.server --bind 127.0.0.1 --port 8456
```

Then hit `/health` to confirm, and see [`examples/QUICKSTART.md`](examples/QUICKSTART.md)
for a full walkthrough of write → govern → query.

---

## Feature Matrix

| Capability | Mímir |
|---|---|
| Event sourcing (immutable events) | ✅ |
| Governance pipeline (LLM assessor) | ✅ |
| Multi-agent federation + ACL | ✅ |
| Vector + FTS + graph fusion (RRF) | ✅ |
| Local CPU embeddings & rerank | ✅ |
| Search-feedback self-evolution | ✅ |
| Ebbinghaus decay + Chronos validity | ✅ |
| Conflict resolution (disputed, never deleted) | ✅ |
| Skill crystallization | ✅ |
| Multi-modal fact assets | ✅ |
| Obsidian wikilink bidirectional linking | ✅ |
| MCP server (27 tools) | ✅ |
| Hermes MemoryProvider plugin | ✅ |
| PyPI + Docker packaging | ✅ |

---

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| v10.0 | In-package governance, Opinion/Observation confidence layer | ✅ shipped |
| v11.0 | Symbolic short-term memory + CodeGraph + reflect/federation API | ✅ shipped |
| v12.0 | Insight: Ebbinghaus decay, Chronos, EvolveMem, recall funnel, conflict resolution, crystallization, MCP, multimodal, PyPI/Docker | ✅ shipped |
| v12+ | Hermes MemoryProvider live integration, retrieval eval baseline | 🔵 in progress |

---

## Security

- API binds `127.0.0.1` only; remote access via reverse proxy (nginx / Cloudflare Tunnel)
- Bearer-token auth with scopes (`read/write/review/manage/admin`) on every endpoint
- SQLite triggers make `memory_events` / `fact_versions` immutable
- Idempotency keys + `actor_principal` + audit logging on all mutations
- `egress_policy=local_only` blocks external processing of sensitive facts

See [SECURITY.md](SECURITY.md) for the vulnerability disclosure policy.

---

## Acknowledgements

Mímir stands on the shoulders of several excellent open-source memory projects.
We are grateful to their authors for ideas we borrowed and built upon:

| Project | Author | What We Learned |
|---|---|---|
| [aiduMEI](https://github.com/monkey2jack/aiduMEI) | [monkey2jack](https://github.com/monkey2jack) | The **governance + self-evolution vision** that shaped Mímir v12 "Insight": Tahoe-Gate relevance gating, the EvolveMem feedback loop, conflict-resolution, and skill-crystallization patterns. This project is the single largest influence on our design. |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | Tencent Cloud | Symbolic short-term memory (Mermaid canvas offload + drill-down) and CodeGraph indexing |
| [Hindsight](https://github.com/obsidianforensics/hindsight) | Obsidian Forensics | Belief modeling — the Opinion/Observation layer that separates "what I know" from "how sure I am" |
| [Mem0](https://github.com/mem0ai/mem0) / [MemGPT](https://github.com/cpacker/MemGPT) | mem0ai / cpacker | The memory-pipeline paradigm: tiered storage, context management, and memory as a first-class service |

**A special note on [aiduMEI](https://github.com/monkey2jack/aiduMEI)** (aidu Memory Engine
Insight, "爱嘟优忆思"): beyond the four borrowed patterns above, its author's deep
thinking on **verbatim preservation vs. distillation** — "蒸馏会丢温度，原文才是证据"
(distillation loses warmth; the verbatim record is the evidence) — directly
inspired Mímir's retention-exemption design, where conversation messages cited
by committed facts are never purged. We are building in the same spirit, and we
encourage you to check out aiduMEI as well.

---

## License

[MIT](LICENSE)
