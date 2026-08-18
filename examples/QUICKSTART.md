# Mímir Quick Start · 快速开始

> English · 中文双语

This guide gets Mímir running locally with zero external dependencies and walks
through the core flow: **write a fact → query it → see it governed**.

本指南让你在零外部依赖的情况下本地跑起 Mímir，并走通核心流程：
**写入事实 → 查询 → 观察治理**。

---

## 1. One-time setup · 一次性配置

```bash
# 1. install the package and embedding extras / 安装包与嵌入依赖
pip install -e ".[embeddings]"

# 2. create a minimal config + secrets layout / 创建最小配置与密钥目录
export MIMIR_HOME=~/.hermes/mimir
export MIMIR_DATA_DIR=$MIMIR_HOME/data
export MIMIR_SECRETS_DIR=$MIMIR_HOME/secrets
export MIMIR_CONFIG_FILE=$MIMIR_HOME/mimir_config.yaml
mkdir -p $MIMIR_DATA_DIR $MIMIR_SECRETS_DIR
```

> Mímir uses local CPU-only embeddings (bge-m3 via sentence-transformers) — no
> API keys required for the core loop. Governance/LLM assessment is optional.
>
> Mímir 使用本地 CPU 嵌入（sentence-transformers 的 bge-m3）——核心闭环无需任何
> API key。治理/LLM 评估是可选的。

## 2. Start the server · 启动服务

```bash
# serve on loopback only (production default) / 仅监听回环地址（生产默认）
mimir-server --bind 127.0.0.1 --port 8456
```

## 3. Write a fact · 写入事实

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

## 4. Query it back · 查询

```bash
curl -X POST http://127.0.0.1:8456/v8/query \
  -H "Content-Type: application/json" \
  -d '{"text": "what is a memory in mimir?", "limit": 5}'
```

You should get the fact back, ranked with a `score_explanation` showing the
vector + FTS + graph fusion and confidence/freshness/decay weighting.

你应该能取回该事实，并看到 `score_explanation` 展示的向量 + 全文 + 图融合，以及
置信度/新鲜度/衰减加权。

## 5. See governance in action · 观察治理

New candidates enter a review queue. Run the governance worker to auto-assess:

新候选会进入审核队列。运行治理 worker 自动评估：

```bash
mimir-worker governance
```

Pending candidates get classified (noise → reject; low-risk → provisional →
fast-track commit; uncertain → human review).

待处理候选会被分类（噪声 → 拒绝；低风险 → 暂定 → 快轨提交；不确定 → 人工审核）。

---

## Core concepts in 30 seconds · 30 秒速览核心概念

| Concept 概念 | Meaning 含义 |
|---|---|
| **Event-sourced** 事件溯源 | Every change is an append-only `memory_events` row; never overwritten 每次变更都是追加式 `memory_events` 行；永不覆盖 |
| **Three layers** 三层 | `memory` (facts 事实), `learning` (methods 方法), `wiki` (docs 文档) |
| **Governed** 治理 | LLM + deterministic policy classify every candidate before commit 提交前由 LLM + 确定性策略分类每个候选 |
| **Query fusion** 查询融合 | Vector + FTS + graph channels merged via RRF 向量 + 全文 + 图通过 RRF 融合 |
| **Self-evolution** 自我演化 | Search feedback nudges fact confidence over time 检索反馈随时间微调事实置信度 |
| **Tombstone forgetting** 墓碑遗忘 | Forgetting never deletes; marks `tombstoned`, hides from active retrieval 遗忘不删除；标记 `tombstoned` 并从活跃检索隐藏 |

---

## Where to look next · 下一步

- `ARCHITECTURE.md` — full system design 完整系统设计
- `docs/MIMIR-v12-GOAL.md` — the v12 roadmap v12 路线图
- `tests/` — 198+ tests that double as behavioral spec 198+ 测试，兼作行为规范
- `hermes-plugin/` — integrate Mímir as a Hermes memory provider 将 Mímir 集成为 Hermes 记忆提供方
