# Mímir — The Local-First Unified Brain for All Your AI Agents & Obsidian

> **One shared memory across multiple agents; one durable knowledge base projected to local Obsidian.**  
> Combining event sourcing, entity graphs, autonomous sensing, and tamper-evident audit chains. Let AI agents collaborate without amnesia, and let personal knowledge continually self-heal.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-21-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README_en.md) · [简体中文](README.md)

---

## Why Mímir

Most memory systems accept whatever an agent says: no review, no ledger, no undo.
Mímir is designed the other way around — **every memory is reviewed, audited, and revocable**:

| Core Feature | Description |
|---|---|
| **Dual-Pool & Bridge Architecture (2.0)** | Subjective conversational context (Memory Pool) physically decoupled from objective technical entities (Knowledge Pool), linked via fact-entity bridge for multi-hop recall. |
| **Governance Pipeline & Physical Gates** | New memories enter a candidate queue with Tier-0/1/2 dedup; `block_strong` physically prevents duplicate writes; includes 72h aging alerts. |
| **Event-Sourced & Tamper-Evident** | The event ledger `memory_events` and `fact_versions` are protected by DB triggers that forbid UPDATE/DELETE; version rows carry a `previous_version_hash` chain; auditable from day one. |
| **Fine-Grained ACL & Federation** | Cross-agent sharing across technical domains while strictly isolating private facts; cross-node Fernet encrypted federation sync. |
| **Honest Telemetry & Budget Honesty** | Queries carry `recall_verdict ∈ {found, not_found, degraded}`; Token overages explicitly enumerate dropped items (no fake stubs). |
| **Obsidian Vault Bridge (1.7.0)** | Facts that pass a four-gate admission check can be published as machine-owned cards (Markdown + YAML frontmatter) with a Git snapshot before and after each write; only a dedicated card folder is written, human-authored text is never touched. |

> **Current Production Release: `1.7.0 — Vault Bridge`**  
> Builds on 1.6.0 (Perception & Crystallization) by completing the human-knowledge side: a declarative publication allowlist, four admission gates, machine card rendering, a self-ingest guard and Git snapshots. It also turns the transaction lock-retry from an unrecorded production hot-patch into a tested behaviour.  
> Full 2.0 design specification: [`docs/superpowers/specs/2026-10-26-mimir-2.0-unified-brain-design.md`](docs/superpowers/specs/2026-10-26-mimir-2.0-unified-brain-design.md).

---

## Quickstart (60 Seconds)

### 1. Launch Mímir Core Service (Local-First)

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory

# One-command bootstrap (installs dependencies, downloads local embeddings, starts service)
./bootstrap.sh
```

Service starts at `http://127.0.0.1:8456`. Verify health:
```bash
curl http://127.0.0.1:8456/health
# Returns: {"status":"ok","service":"mimir-api","version":"1.7.0","schema_version":21}
```

### 2. Connect Your AI Agents

#### Option A: Claude Code / Cursor / Windsurf (via standard MCP)
Add Mímir with one command:
```bash
claude mcp add mimir -- mimir-mcp
```
Your agent immediately gains `mimir_query`, `mimir_write`, and `mimir_bootstrap` native tools.

#### Option B: Hermes Agent Native Integration
Enable the global memory provider in `~/.hermes/config.yaml`:
```yaml
memory:
  provider: mimir_memory_provider
```
The primary agent (`Heimdallr-EX`) automatically prefetches recall context before every dialogue turn.

#### Option C: Custom Agents (Zero-Config Header Onboarding)
Simply send an identity header with your request:
```bash
curl -H "Authorization: Bearer <TOKEN>" \
     -H "X-Mimir-Agent: my-custom-agent" \
     http://127.0.0.1:8456/v15/bootstrap
```
Mímir dynamically enrolls the agent and returns active iron rules (L0), user preferences (L1), and domain inventories.

---

## Architecture: Dual-Pool & Bridge (Mímir 2.0)

Mímir 2.0 decouples machine-facing context from human-readable knowledge while joining them via multi-hop graph bridges:

```
                  ┌─────────────────────────────────────────────────────────┐
                  │                 Universal Agent Front                   │
                  │   Claude Code │ Hermes (Heimdallr-EX) │ Codex │ Cursor │ REST │
                  └────────────────────────────┬────────────────────────────┘
                                               │ (Unified MCP / Zero-Config SDK)
                                               ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │              Mímir Core Coordination Engine             │
                  │  • Semantic Routing  • Dedup Gates  • Budget Honesty    │
                  └──────────────┬───────────────────────────┬──────────────┘
                                 │                           │
          ┌──────────────────────▼──────┐             ┌──────▼──────────────────────┐
          │     A. Memory Pool          │             │     B. Knowledge Pool       │
          │   (Subjective, Contextual)  │             │    (Objective, Structured)  │
          ├─────────────────────────────┤             ├─────────────────────────────┤
          │ • Session Logs & Intention  │             │ • Entity Graph (Entities)   │
          │ • Iron Rules & Prefs (L0-L1)│ Bridge Index│ • Runbooks & Tech Specs     │
          │ • Execution Traces          │◄───────────►│ • Monitored Assets & Probes │
          │ • Immutable Event Sourcing  │(fact_entity)│ • Bi-temporal Decay (L4-L5) │
          └──────────────┬──────────────┘             └──────────────┬──────────────┘
                         │                                           │
                         │                                           ▼
                         │                            ┌─────────────────────────────┐
                         │                            │      Human Face             │
                         │                            │   【Local Obsidian Vault】   │
                         │                            ├─────────────────────────────┤
                         │                            │ • Daily Tech Briefs         │
                         │                            │ • Tool Cards ([[Wikilinks]])│
                         └───────────────────────────►│ • Personal Notes (Protected)│
                                                      └─────────────────────────────┘
```

---

## The Four Pillars

### 1. Federated Memory & Cross-Agent Sharing
- **Break Context Silos**: Technical domain facts (`infrastructure`, `knowledge`, `tech_support`) are shared across authenticated agents; private domains (`personal`) remain strictly owner-isolated.
- **Principal Alias Ladder**: Primary agent `Heimdallr-EX` (`heimdallr`) inherits all 469 legacy facts and permissions formerly under `mentor` with zero database migrations.
- **Cross-Node CRDT Federation**: Multiple independent Mímir instances synchronize designated keys via encrypted event ledgers (see [docs/FEDERATION.md](docs/FEDERATION.md)).

### 2. Structured Knowledge & Autonomous Sensing
- **Daily Brief Sensing**: Background daemons pull GitHub Releases, technical feeds, and free model probes every morning.
- **Dual-Layer Crystallization**: Generates both human-readable Markdown digests and machine-executable `ToolCard` and `Runbook` entities.
- **Debt-Replay Mechanism**: Gracefully handles upstream LLM 429 rate limits by queuing raw feeds and auto-replaying distillation once connectivity recovers.

### 3. Obsidian Vault Bridge (1.7.0)
- **Declarative publication allowlist**: `mimir_v8/publication_targets.yaml` names the folder roots that may be published to and the forbidden ones (including `60-敏感信息`); validated at load time, rejecting overlaps, path traversal and disallowed suffixes (fail-closed).
- **Four admission gates** (`is_publishable_fact`): `sensitivity: restricted` is refused, then `visibility: all`, then an empty body, then credential-shaped content (same `redact_text` the learning pipeline uses); external targets additionally need `egress_policy=external_allowed`. Missing labels are refused too.
- **Machine cards** (`vault_card.render_card`): frontmatter carries `mimir_generated: true` and `mimir_id`; machine-owned text sits between `mimir:managed` markers; stable filename `<entity_id>.md`; writes go only to the dedicated card folder.
- **Human content protection**: `publish_entity` returns `human_content_changed` and skips when a human edited the non-managed part of a card; callers other than the vault adapter principal are rejected.
- **Self-ingest guard**: the vault collector reads the first 4096 bytes of each file, recognises `mimir_generated` cards and skips them, so machine output is never re-ingested as new memory.
- **Git snapshots**: one local Git commit before and one after each publish (`vault_git.GitProtector`), enabling rollback.
- **Current boundary**: `publish_entity` is a library-level API. It is not yet wired into background scheduling and has no HTTP endpoint. There is no `ObsidianProjector`; the existing projectors are FTS / CoreMemory / Graph / Vector.

### 4. Hardened Governance & Honest Telemetry
- **Tier-0/1/2 Dedup**: O(1) hash merging + gated fuzzy Jaccard matching + physical blocking via `MIMIR_DEDUP_GATE=block_strong`.
- **72h Stale Backlog Alerts**: Surfaces stuck `human_review` items to eliminate silent backlogs.
- **Budget Honesty Protocol**: Query overages explicitly return `budget_exceeded` and dropped counts (no fake stubs).
- **Progressive Disclosure**: Server-minted HMAC cursors stream context across Index (~50 tokens) → Timeline → Full Content (~500 tokens).

---

## Benchmarks & Quality Protocol

Mímir follows three strict benchmarking rules: **methodology travels with metrics, commitments must be realized on time, and error logs are published alongside aggregates**.

**Golden 24 Benchmark (Live Production Service, Standard Profile)**:

| Metric | Score | Floor | Status |
|---|---|---|---|
| **hit@1** | **0.750** | — | High-precision top match |
| **hit@3** | **0.833** | 0.650 | ✅ Exceeds baseline |
| **hit@5** | **0.875** | — | Comprehensive coverage |
| **hit@10** | **0.917 ~ 1.000** | 0.830 | ✅ Production proven |
| **MRR** | **0.786** | — | Reliable ranking quality |

---

## Roadmap

| Version | Codename | Core Milestones | Status |
|:---|:---|:---|:---|
| **v1.3.0** | Ecosystem | RECALL Funnel, Temporal Ingestion, Dedup Activation, Reflect | ✅ Released |
| **v1.4.0** | Legibility | Machine-readable explanations, Progressive Disclosure (HMAC), Budget Honesty | ✅ Released |
| **v1.4.1** | Extraction Fix | Bidirectional conversation context, assistant reply ingestion | ✅ Released |
| **v1.5.0** | Federation & Sharing | Silo elimination, mentor retirement, zero-config onboarding | ✅ Released |
| **v1.6.0** | Perception & Crystallization | Entity knowledge base, declarative probe & source registry, double-layer brief crystallization | ✅ Released |
| **v1.7.0** | Vault Bridge | Publication allowlist, four admission gates, machine cards, self-ingest guard, Git snapshots, transaction lock-retry | ✅ Released · **In Production** |
| **v2.0.0** | The Unified Brain | One-command CLI/Docker distribution, Command Center Dashboard | 🎯 Target (Milestone #6) |

---

## The Name

Named after **Mímir**, the figure in Norse mythology renowned for his wisdom and knowledge, who guards the well of wisdom beneath the root of Yggdrasil. Odin sacrificed an eye to drink from this well.

In modern multi-agent systems, Mímir stands as that guardian: resisting ephemeral model hallucinations, anchoring factual truth, and providing durable intelligence to all connected agents.

---

## License

Licensed under the [MIT License](LICENSE).
