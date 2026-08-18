# Mímir Quick Start

This guide gets Mímir running locally with zero external dependencies and walks
through the core flow: **write a fact → query it → see it governed**.

---

## 1. One-time setup

```bash
# 1. install the package and embedding extras
pip install -e ".[embeddings]"

# 2. create a minimal config + secrets layout
export MIMIR_HOME=~/.hermes/mimir
export MIMIR_DATA_DIR=$MIMIR_HOME/data
export MIMIR_SECRETS_DIR=$MIMIR_HOME/secrets
export MIMIR_CONFIG_FILE=$MIMIR_HOME/mimir_config.yaml
mkdir -p $MIMIR_DATA_DIR $MIMIR_SECRETS_DIR
```

> Mímir uses local CPU-only embeddings (bge-m3 via sentence-transformers) — no
> API keys required for the core loop. Governance/LLM assessment is optional.

## 2. Start the server

```bash
# serve on loopback only (production default)
mimir-server --bind 127.0.0.1 --port 8456
```

## 3. Write a fact

```bash
curl -X POST http://127.0.0.1:8456/v8/facts \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Mímir treats every memory as an immutable, governed event.",
    "domain": "knowledge",
    "fact_type": "pattern",
    "visibility": "all",
    "sensitivity": "internal",
    "egress_policy": "local_only"
  }'
```

## 4. Query it back

```bash
curl -X POST http://127.0.0.1:8456/v8/query \
  -H "Content-Type: application/json" \
  -d '{"text": "what is a memory in mimir?", "limit": 5}'
```

You should get the fact back, ranked with a `score_explanation` showing the
vector + FTS + graph fusion and confidence/freshness/decay weighting.

## 5. See governance in action

New candidates enter a review queue. Run the governance worker to auto-assess:

```bash
mimir-worker governance
```

Pending candidates get classified (noise → reject; low-risk → provisional →
fast-track commit; uncertain → human review).

---

## Core concepts in 30 seconds

| Concept | Meaning |
|---|---|
| **Event-sourced** | Every change is an append-only `memory_events` row; never overwritten |
| **Three layers** | `memory` (facts), `learning` (methods), `wiki` (docs) |
| **Governed** | LLM + deterministic policy classify every candidate before commit |
| **Query fusion** | Vector + FTS + graph channels merged via Reciprocal Rank Fusion (RRF) |
| **Self-evolution** | Search feedback nudges fact confidence over time (EvolveMem) |
| **Tombstone forgetting** | Forgetting never deletes; it marks `tombstoned` and hides from active retrieval |

---

## Where to look next

- `ARCHITECTURE.md` — full system design
- `docs/MIMIR-v12-GOAL.md` — the v12 roadmap
- `tests/` — 198+ tests that double as behavioral spec
- `hermes-plugin/` — integrate Mímir as a Hermes memory provider
