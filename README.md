# Mímir — Federated Memory for Multi-Agent Systems

> **One shared memory, many agents.** An event-sourced, self-evolving,
> federated memory system that lets multiple AI agents remember *together* —
> and forget intelligently.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-20-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README.md) · [中文](README_zh.md)

---

## The Name

**Mímir** originates from Norse mythology — the guardian of the Well of Mímir (Mímisbrunnr), the source of wisdom.

In Norse mythology, Odin, the Allfather, sacrificed one of his own eyes to drink from the Well of Wisdom. The well possessed wisdom precisely because it was guarded day and night by the giant **Mímir** — the personification of memory and knowledge. Odin lost an eye, yet gained the wisdom to foresee the future. Even after Ragnarök, Mímir's severed head remained by Odin's side, continuing to offer him counsel.

This name is an apt metaphor for a memory system:

- **Memory comes at a cost** — Odin traded an eye for wisdom, just as reliable memory demands the continuous investment of governance, audit, and evolution, rather than a cheap "just write it down".
- **Memory endures** — Even when the world ends (Ragnarök), Mímir remains. A true memory system should withstand the passage of time and version iterations, rather than vanishing with a process restart.
- **The value of memory lies in being "consumed"** — A well without drinkers is merely water. If memory cannot be retrieved at the right moment with the proper permissions, it remains mere accumulated data.

Mímir is therefore not merely a technical name, but a design promise: **to build a long-lived, consumable memory substrate worthy of trading "an eye" for.**

---

## The Philosophy

Mímir is founded on a simple yet often overlooked conviction: **memory is not a static accumulation of data, but a living lifecycle process.**

Most memory systems treat "remembering" as the destination — store it, retrieve it, done. But real-world memory does not work this way. Genuine memory:

1. **Is admitted with deliberation** — Not all information is worth remembering, nor should everything "remembered" be unconditionally trusted. Every candidate must undergo governance assessment before admission, keeping extraction strictly separated from approval.
2. **Evolves over time** — Frequently used memories gain trust, while obsolete ones are deweighted. Confidence should be a dynamic curve over time, not a frozen scalar.
3. **Knows how to forget** — Forgetting is not the enemy of memory, but an essential part of it. True forgetting is selective letting go rather than outright destruction — using tombstones rather than deletion, and Ebbinghaus curves rather than blunt purges.
4. **Has ownership and boundaries** — In a world shared by multiple agents, *who remembers* and *who can view* are just as crucial as *what was remembered*. Memory must have clear ownership, boundaries, and deliberate sharing.

This is the core philosophy of Mímir: **treating memory as something to be respected, governed, evolved, and consciously forgotten, rather than an unbounded hash table.**

We do not strive to remember the most; we strive to remember what matters.

---

## The One Thing Mímir Does That Others Don't: **Federated Memory**

Most memory systems are built for **one agent**. Mímir is built for **many**.

In a multi-agent system — a network ops agent, a quant-trading agent, a tech consultant, a trainer — each agent has a different job, different knowledge, and a different owner. They shouldn't all see everything, but they *should* be able to share what matters.

Mímir's answer is **federated memory with fine-grained isolation**:

- **Each agent has its own memory** — facts are tagged with `owner_principal`, and ACLs control exactly who can read what.
- **Agents share deliberately** — three visibility tiers (`all` / `shared` / `owner_only`) let you mark a fact as "mine alone", "for my team", or "public to all agents".
- **Cross-agent awareness** — an awareness broadcast surfaces what other agents learned recently, so agents don't operate in silos.
- **Federated search** — `/v10/federation/{peer}` queries across principals with ACL enforcement, so one agent can ask "what does anyone know about X?" safely.

The result: **a single memory substrate shared by N agents, with the isolation of N private memories.** That's the difference between a memory store and a *collective* memory.

---

## Why Mímir Is Different (Beyond Federation)

Federation is the headline. But Mímir is also built on a fundamentally different premise from "a vector database with a nice API": **a memory is an event, not a row.**

| A normal memory store | Mímir |
|---|---|
| Built for one agent | **Built for N agents with ACL-isolated federation** |
| Overwrites old memories | **Appends immutable events** — history is never rewritten |
| "Forgetting" = deleting rows | **Tombstone forgetting** — marked, never destroyed |
| Memory quality = your prompt | **Governed** — an LLM *evaluates* every candidate; it can only *suggest*, never *commit* |
| Static retrieval score | **Self-evolving** — feedback nudges confidence up *and* down |
| Single vector index | **Three-channel fusion** — vector + FTS + graph, RRF + local rerank |
| Facts decay arbitrarily | **Ebbinghaus decay** — five forgetting curves, never-forget to ephemeral |

Mímir is the complete lifecycle for collective agent memory:
*ingest → govern → commit → retrieve → self-correct → forget* — every step auditable and reversible.

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
                         │   owner_principal + ACL      │
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
                          (per-agent visibility enforced)
```

---

## The Six Pillars

### 1. Federated Memory — *the headline*
Multiple agents share one memory substrate with `owner_principal` isolation, three visibility tiers, cross-agent awareness, and federated search with ACL. See [docs/FEDERATION.md](docs/FEDERATION.md) for the multi-agent setup guide.

### 2. Event-Sourced Truth
Every fact is an append-only event stream. `memory_events` and `fact_versions` are trigger-protected against UPDATE and DELETE — rewindable, auditable, and explainable as a structural property.

### 3. Governed Ingestion
A deterministic rule engine plus an independent LLM assessor classify every candidate before commit. The LLM is **deliberately separated** from the commit path — it cannot extract *and* approve.

### 4. Symmetric Self-Evolution
Search feedback (`useful`/`useless`/`correction`) aggregates over a 7-day window and nudges confidence up *and* down, gated by minimum signal count.

### 5. Scientific Forgetting
Five Ebbinghaus decay tiers + Chronos dual-timeline. Identity rules never decay; ephemeral facts half-life in 7 days; expired facts are deweighted — never deleted.

### 6. Local-First Privacy
All embeddings (bge-m3) and reranking (ms-marco) run **locally on CPU** — embedded text never leaves your machine. API binds `127.0.0.1` only.

---

## Quick Start (Out of the Box)

**One command** — installs dependencies, bootstraps config & tokens, and starts the server:

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory
./bootstrap.sh
```

That's it. `bootstrap.sh` does three things:
1. `pip install -e ".[embeddings]"` — installs deps (first run downloads bge-m3)
2. `scripts/init.sh` — creates dirs, agent tokens, minimal config
3. starts the server on `127.0.0.1:8456`

Then hit `curl http://127.0.0.1:8456/health` to confirm.

> **Manual setup** (if you prefer control):
> `pip install -e ".[embeddings]"` → `./scripts/init.sh` → `python -m mimir_v8.server ...`
>
> **For a multi-agent federated setup**, follow [docs/FEDERATION.md](docs/FEDERATION.md).

---

## Feature Matrix

| Capability | Mímir |
|---|---|
| **Multi-agent federated memory + ACL isolation** | ✅ |
| Cross-agent awareness broadcast | ✅ |
| Federated cross-principal search | ✅ |
| **Cross-node CRDT federation (Lamport LWW + Fernet envelopes)** | ✅ |
| Event sourcing (immutable events) | ✅ |
| Governance pipeline (LLM assessor) | ✅ |
| Vector + FTS + graph fusion (RRF) | ✅ |
| Local CPU embeddings & rerank | ✅ |
| Search-feedback self-evolution | ✅ |
| Ebbinghaus decay + Chronos validity | ✅ |
| L0–L3 tiered memory with progressive disclosure | ✅ |
| Anchor channel (iron rules / core prefs never voted out) | ✅ |
| Shared agent blackboards (distill to facts) | ✅ |
| Temporal knowledge graph (valid_during history) | ✅ |
| Proactive intent-based wake | ✅ |
| Conflict resolution (disputed, never deleted) | ✅ |
| **Mímir-Eval: standard benchmark suite (HitRate@K · MRR · ACL-leak, golden-set floors)** | ✅ |
| Skill crystallization | ✅ |
| **AutoSkill: traces → wiki → L3 skills (auto-compiled)** | ✅ |
| **Cross-model projection (tier-aware injection blocks)** | ✅ |
| Multi-modal fact assets | ✅ |
| Obsidian wikilink bidirectional linking | ✅ |
| MCP server (27 tools) | ✅ |
| Hermes MemoryProvider plugin | ✅ |
| Dashboard (13-tab web UI) | ✅ |
| PyPI + Docker packaging | ✅ |

---

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| v10.0 | In-package governance, Opinion/Observation confidence layer | ✅ shipped |
| v11.0 | Symbolic short-term memory + CodeGraph + reflect/federation API | ✅ shipped |
| v12.0 | Insight: Ebbinghaus decay, Chronos, EvolveMem, recall funnel, conflict resolution, crystallization, MCP, multimodal, PyPI/Docker | ✅ shipped |
| v12.1 | Mímir-Eval benchmark suite (HitRate@K/MRR/ACL-leak, golden-set floors), full-source ingestion, dynamic agent/domain registry | ✅ shipped |
| v12.2 | L0–L3 tiered memory, unified Profile API, XTMEM lineage, anchor channel, /v12/profile | ✅ shipped |
| v13.0 | Multi-agent shared blackboards, temporal knowledge graph, proactive intent wake | ✅ shipped |
| v14.0 | AutoSkill pipeline, cross-node CRDT federation, cross-model projection | ✅ shipped · **in production since 2026-09-03** |
| v14.1 | Quality & resilience: golden-set stewardship, first production skill crystallization, resilience gears, honest telemetry | 🔵 in progress |

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

Mímir stands on the shoulders of several excellent open-source memory projects. We are grateful to their authors for ideas we borrowed and built upon:

| Project | Author | What We Learned |
|---|---|---|
| [aiduMEI](https://github.com/monkey2jack/aiduMEI) | [monkey2jack](https://github.com/monkey2jack) | The **governance + self-evolution vision** that shaped Mímir v12 "Insight": Tahoe-Gate relevance gating, the EvolveMem feedback loop, conflict-resolution, and skill-crystallization patterns. The single largest influence on our design. |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | Tencent Cloud | Symbolic short-term memory (Mermaid canvas offload + drill-down) and CodeGraph indexing |
| [Hindsight](https://github.com/obsidianforensics/hindsight) | Obsidian Forensics | Belief modeling — the Opinion/Observation layer separating "what I know" from "how sure I am" |
| [Mem0](https://github.com/mem0ai/mem0) / [MemGPT](https://github.com/cpacker/MemGPT) | mem0ai / cpacker | The memory-pipeline paradigm: tiered storage, context management, memory as a first-class service |

**A special note on [aiduMEI](https://github.com/monkey2jack/aiduMEI)** (aidu Memory Engine Insight): beyond the four borrowed patterns, its author's deep thinking on **verbatim preservation vs. distillation** — *"distillation loses warmth; the verbatim record is the evidence"* — directly inspired Mímir's retention-exemption design, where conversation messages cited by committed facts are never purged. We encourage you to check out aiduMEI.

---

## License

[MIT](LICENSE)

---

## Contact

Maintainer: **sandro1123** · 📧 [sandro1123@hotmail.com](mailto:sandro1123@hotmail.com)
