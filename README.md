# Mímir — Federated Memory for Multi-Agent Systems

> **One shared memory, many agents.** An event-sourced, self-evolving,
> federated memory system that lets multiple AI agents remember *together* —
> and forget intelligently.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-18-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README.md) · [中文](README_zh.md)

---

## The Name · 名字的由来

**Mímir**（密米尔）源自北欧神话 —— 智慧之泉（Well of Mímir）的守护者。

在北欧神话中，众神之父奥丁（Odin）为了换取一口智慧之泉的井水，献出了自己
的一只眼睛。而这口泉水之所以蕴含智慧，正是因为它由巨人 **Mímir** 日夜守护——
Mímir 本身就是「记忆」与「知识」的化身。奥丁失去一只眼，却得到了预见未来的
智慧；而 Mímir 的头颅，即使在诸神黄昏（Ragnarök）之后，仍被奥丁带在身边，
继续为他提供忠告。

这个名字对一套记忆系统而言，是一个恰到好处的隐喻：

- **记忆需要代价** —— 奥丁用一只眼换智慧，正如可靠的记忆需要投入治理、
  审计与演化的成本，而非廉价的「记下来就行」。
- **记忆是长存的** —— 即使世界毁灭（诸神黄昏），Mímir 仍在。真正的记忆
  系统应当经得起时间的冲刷、版本的更迭，而不是随进程重启而消失。
- **记忆的价值在于「被喝下」** —— 泉水若无人饮用便只是水。记忆若无法在
  正确的时刻、以正确的权限被检索到，就只是堆积的数据。

Mímir 因此不只是一个技术命名，而是一句设计承诺：**做一套值得用「一只眼睛」
去换的、长存的、可饮用的记忆。**

---

## The Philosophy · 我们的哲学

Mímir 建立在一个简单但常被忽视的信念上：**记忆不是数据的堆积，而是一个
有生命周期的过程。**

大多数记忆系统把「记住」当成终点——存进去，取出来，完事。但真实世界的
记忆不是这样的。真实记忆会：

1. **被审慎地接纳** —— 不是所有信息都值得记住，也不是所有「记住了」都
   该被无条件信任。所以我们让每一条候选在进入记忆之前，都经过治理评估，
   并让「提取」与「批准」永远分离。
2. **随时间演化** —— 用得多的记忆更可信，失效的记忆被降权。记忆的
   置信度应该是一段随时间变化的曲线，而不是一个静止的数字。
3. **懂得遗忘** —— 遗忘不是记忆的敌人，而是记忆的一部分。真正的遗忘
   是「有选择地放下」，而不是「销毁」。所以我们用墓碑标记而非删除，
   用艾宾浩斯曲线而非一刀切。
4. **有归属、有边界** —— 在多个智能体共享的世界里，「谁记得」和
   「谁能看」与「记住了什么」同样重要。记忆必须有主人，有边界，有
   克制的共享。

这就是 Mímir 的全部哲学：**把记忆当作一件需要被尊重、被治理、被演化、
被有意识地遗忘的事物，而不是一个可以无限写入的哈希表。**

我们不追求「记得最多」，我们追求「记得恰到好处」。

---

## The One Thing Mímir Does That Others Don't: **Federated Memory**

Most memory systems are built for **one agent**. Mímir is built for **many**.

In a multi-agent system — a network ops agent, a quant-trading agent, a tech
consultant, a trainer — each agent has a different job, different knowledge, and
a different owner. They shouldn't all see everything, but they *should* be able
to share what matters.

Mímir's answer is **federated memory with fine-grained isolation**:

- **Each agent has its own memory** — facts are tagged with `owner_principal`,
  and ACLs control exactly who can read what.
- **Agents share deliberately** — three visibility tiers (`all` / `shared` /
  `owner_only`) let you mark a fact as "mine alone", "for my team", or "public
  to all agents".
- **Cross-agent awareness** — an awareness broadcast surfaces what other agents
  learned recently, so agents don't operate in silos.
- **Federated search** — `/v10/federation/{peer}` queries across principals with
  ACL enforcement, so one agent can ask "what does anyone know about X?" safely.

The result: **a single memory substrate shared by N agents, with the isolation
of N private memories.** That's the difference between a memory store and a
*collective* memory.

---

## Why Mímir Is Different (Beyond Federation)

Federation is the headline. But Mímir is also built on a fundamentally different
premise from "a vector database with a nice API": **a memory is an event, not a
row.**

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
*ingest → govern → commit → retrieve → self-correct → forget* — every step
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

### 1. Federated Memory (多 Agent 联邦记忆) — *the headline*
Multiple agents share one memory substrate with `owner_principal` isolation,
three visibility tiers, cross-agent awareness, and federated search with ACL.
See [docs/FEDERATION.md](docs/FEDERATION.md) for the multi-agent setup guide.

### 2. Event-Sourced Truth (事件溯源)
Every fact is an append-only event stream. `memory_events` and `fact_versions`
are trigger-protected against UPDATE and DELETE — rewindable, auditable, and
explainable as a structural property.

### 3. Governed Ingestion (治理闭环)
A deterministic rule engine plus an independent LLM assessor classify every
candidate before commit. The LLM is **deliberately separated** from the commit
path — it cannot extract *and* approve.

### 4. Symmetric Self-Evolution (检索自进化)
Search feedback (`useful`/`useless`/`correction`) aggregates over a 7-day window
and nudges confidence up *and* down, gated by minimum signal count.

### 5. Scientific Forgetting (科学的遗忘)
Five Ebbinghaus decay tiers + Chronos dual-timeline. Identity rules never decay;
ephemeral facts half-life in 7 days; expired facts are deweighted — never deleted.

### 6. Local-First Privacy (本地隐私)
All embeddings (bge-m3) and reranking (ms-marco) run **locally on CPU** —
embedded text never leaves your machine. API binds `127.0.0.1` only.

---

## Quick Start (Out of the Box)

**One command** — installs dependencies, bootstraps config & tokens, and starts
the server:

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
| Event sourcing (immutable events) | ✅ |
| Governance pipeline (LLM assessor) | ✅ |
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
| Dashboard (9-tab web UI) | ✅ |
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
| [aiduMEI](https://github.com/monkey2jack/aiduMEI) | [monkey2jack](https://github.com/monkey2jack) | The **governance + self-evolution vision** that shaped Mímir v12 "Insight": Tahoe-Gate relevance gating, the EvolveMem feedback loop, conflict-resolution, and skill-crystallization patterns. The single largest influence on our design. |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | Tencent Cloud | Symbolic short-term memory (Mermaid canvas offload + drill-down) and CodeGraph indexing |
| [Hindsight](https://github.com/obsidianforensics/hindsight) | Obsidian Forensics | Belief modeling — the Opinion/Observation layer separating "what I know" from "how sure I am" |
| [Mem0](https://github.com/mem0ai/mem0) / [MemGPT](https://github.com/cpacker/MemGPT) | mem0ai / cpacker | The memory-pipeline paradigm: tiered storage, context management, memory as a first-class service |

**A special note on [aiduMEI](https://github.com/monkey2jack/aiduMEI)** (aidu Memory
Engine Insight, "爱嘟优忆思"): beyond the four borrowed patterns, its author's deep
thinking on **verbatim preservation vs. distillation** — "蒸馏会丢温度，原文才是证据"
(distillation loses warmth; the verbatim record is the evidence) — directly
inspired Mímir's retention-exemption design, where conversation messages cited
by committed facts are never purged. We encourage you to check out aiduMEI.

---

## License

[MIT](LICENSE)

## Contact

Maintainer: **sandro1123** · 📧 [sandro1123@hotmail.com](mailto:sandro1123@hotmail.com)
