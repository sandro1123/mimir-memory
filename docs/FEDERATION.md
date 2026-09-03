# Multi-Agent Federation Guide · 多智能体联邦接入指南

> English · 中文双语

This guide covers **two levels of federation** in Mímir:

本指南覆盖 Mímir 的**两级联邦**：

1. **Single-instance multi-agent federation 单实例多智能体联邦** (v10+) — multiple
   agents share one Mímir instance with `owner_principal` isolation and ACLs.
   多个智能体共享一个 Mímir 实例，`owner_principal` 隔离 + ACL。
   → This is what the rest of this guide describes.
   → 这正是本指南主体所描述的模式。
2. **Cross-node CRDT federation 跨节点 CRDT 联邦** (v14) — multiple Mímir
   *nodes* (separate instances/databases) replicate designated keys through an
   append-only encrypted event ledger. See the last section.
   多个 Mímir **节点**（独立实例/独立库）通过追加式加密事件账本复制指定键。
   见末节。

---

## Part I · Single-Instance Federation 第一部分：单实例联邦

This guide walks you through connecting **multiple AI agents** to one Mímir
instance so they share memory deliberately — with per-agent isolation.

本指南带你将**多个 AI 智能体**接入同一个 Mímir 实例，让它们在细粒度隔离下
有意识地共享记忆。

---

## 核心概念 · Core Concepts

Mímir 的联邦模型基于三个概念 · Mímir's federation model rests on three ideas:

1. **Owner isolation 归属隔离** — 每条事实有 `owner_principal`，默认只能被其
   所属智能体读取。Each fact has an `owner_principal`; by default only its owner
   can read it.
2. **Visibility tiers 可见性分级** — 三档控制共享范围 · three tiers control
   sharing scope:
   - `owner_only` — 仅自己 · only the owner
   - `shared` — 共享给指定智能体 · shared with specific agents
   - `all` — 所有智能体可见 · visible to all agents
3. **Federation search 联邦搜索** — 跨主体查询，ACL 强制 · cross-principal
   search with ACL enforcement.

---

## 快速开始 · Quick Start

### 第 1 步：初始化 · Step 1 — Bootstrap

```bash
./scripts/init.sh
```

这会生成 · This creates:
- `$MIMIR_HOME/data/` — 数据目录 · data dir
- `$MIMIR_HOME/secrets/api_tokens.json` — 服务端 token 注册表 · server token registry
- `$MIMIR_HOME/secrets/clients/<agent>.token` — 每个智能体的明文 token · plaintext token per agent
- `$MIMIR_HOME/mimir_config.yaml` — 默认 4 智能体配置 · default 4-agent config

### 第 2 步：启动服务 · Step 2 — Start the server

```bash
python -m mimir_v8.server --bind 127.0.0.1 --port 8456 \
  --data-dir $MIMIR_HOME/data \
  --token-file $MIMIR_HOME/secrets/api_tokens.json
```

### 第 3 步：配置每个智能体 · Step 3 — Configure each agent

每个智能体用一个 `principal_id` + 对应 token 连接。以 Hermes 智能体为例：

Each agent connects with a `principal_id` + its matching token. For a Hermes
agent:

```bash
# 1. 安装 MemoryProvider 插件 · install the plugin
./hermes-plugin/install.sh

# 2. 在 Hermes config 里配置 · configure in Hermes config
#    memory.provider = mimir_memory_provider
#    plugins.enabled 加上 mimir_memory_provider · add to plugins.enabled

# 3. 设置该智能体的 token · set the agent's token
export MIMIR_V8_URL=http://127.0.0.1:8456
export MIMIR_AGENT=jarvis           # 该智能体的 principal_id · this agent's id
export MIMIR_V8_TOKEN=$(cat $MIMIR_HOME/secrets/clients/jarvis.token)
```

> 对非 Hermes 智能体，直接调用 REST API，`Authorization: Bearer <token>`。
> For non-Hermes agents, call the REST API directly with `Authorization: Bearer <token>`.

---

## 共享记忆 · Sharing Memory

### 写入一条共享事实 · Write a shared fact

```bash
curl -X POST http://127.0.0.1:8456/v8/facts \
  -H "Authorization: Bearer $JARVIS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "NAS 位于 192.168.1.100，DS923+ 型号",
    "domain": "infrastructure",
    "fact_type": "project_config",
    "visibility": "shared",       # 共享给团队 · shared with the team
    "sensitivity": "internal"
  }'
```

### 跨智能体联邦搜索 · Federated search across agents

```bash
# 问"谁有关于 NAS 的记忆" · ask "who knows about NAS?"
curl -X POST http://127.0.0.1:8456/v10/federation/all \
  -H "Authorization: Bearer $MENTOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "NAS 存储"}'
```

---

## 智能体订阅 · Agent Subscriptions

`mimir_config.yaml` 里每个智能体可声明它关心哪些 domain/type：

In `mimir_config.yaml`, each agent declares which domains/types it subscribes to:

```yaml
agents:
- id: quantmaster
  name: QuantMaster
  role: 量化投顾
  subscriptions:
    domains: [quant, system, knowledge]   # 只订阅量化相关 · only quant-related
    types: [iron_rule, pattern, project_config]
```

这样，跨智能体感知广播（awareness）只会把相关动态推给订阅了对应 domain 的智能体。

This way, the awareness broadcast only surfaces relevant updates to agents
subscribed to the matching domain.

---

## 隔离与权限 · Isolation & Permissions

| 场景 Scenario | 做法 How |
|---|---|
| 完全私有 private | `visibility: owner_only` |
| 团队共享 shared with team | `visibility: shared` + resource_grants 授权 |
| 全员可见 public to all | `visibility: all` |
| 敏感不外发 no external egress | `egress_policy: local_only` |
| 受限 restricted | `sensitivity: restricted`（强制 local_only）|

---

## 下一步 · Next Steps

- 单智能体快速上手 · single-agent quickstart: [examples/QUICKSTART.md](../examples/QUICKSTART.md)
- 完整架构 · full architecture: [ARCHITECTURE.md](../ARCHITECTURE.md)
- 安全政策 · security policy: [SECURITY.md](../SECURITY.md)

---

# Part II · Cross-Node CRDT Federation 第二部分：跨节点 CRDT 联邦 (v14)

> Multiple Mímir **instances** replicating designated state without a center.
> 多个 Mímir **实例**在无中心的前提下复制指定状态。

## When to use which · 何时用哪一级

| 场景 Scenario | 用哪一级 Level |
|---|---|
| 多个智能体，一个服务器，要 ACL 隔离 · many agents, one server, ACL isolation | Part I 单实例联邦 |
| 多台机器各跑一个 Mímir，要跨机器同步 · several machines each running Mímir, cross-machine sync | Part II 跨节点 CRDT |
| 两者都有 · both | 叠加使用 · combine both |

## Design · 设计

```
federation_events (追加式账本 · append-only ledger)
  (crdt_key, lamport, node_id) 唯一身份 → 重投递幂等 · re-delivery is a no-op
  op ∈ {'set','delete'} · value 经 Fernet 信封加密 · value inside a Fernet envelope

federation_peers (节点注册表 · peer registry)
  node_id → public_key 指纹 · fingerprint
```

- **Lamport LWW** — 冲突以 `(lamport, node_id)` 定序，Last-Writer-Wins；
  无需中心仲裁，账本可回放。
- **Fernet 信封** — 事件 value 加密后出节点，节点间以密钥指纹互认
  （`encrypt_envelope` / `ingest_envelope`）。
- **无 REST 面** — 这是节点对节点协议（`FederationService` 库接口：
  `append_event` / `export_events` / `ingest_envelope` / `crdt_state`），不暴露
  HTTP；运维侧用 dashboard 联邦页做只读普查。

## Wire-up · 接线

```python
from mimir_v8.federation import FederationService, encrypt_envelope

svc = FederationService(store, node_id="node-a")   # 首跑自动建两张表
svc.register_peer("node-b", peer_public_key)         # 注册对端指纹

# 发布 · publish: 追加一条事件进账本 ((crdt_key, lamport, node_id) 幂等)
event = {
    "event_id": ..., "crdt_key": "shared/skill/k8s-drain",
    "lamport": next_lamport, "node_id": "node-a", "op": "set",
    "value": encrypt_envelope(payload, key), "recorded_at": now,
}
svc.append_event(event)

# 同步 · sync: 导出自某水位以来的事件（给对端），对端回灌 (Lamport LWW)
bundle = svc.export_events(since=last_seq, to_peer="node-b")
# 对端执行: svc.ingest_envelope(bundle)  — 重投递 no-op

# 读取收敛态 · read converged state
svc.crdt_state("shared/skill/k8s-drain")
```

监控 · observe: dashboard → 联邦页（节点注册表 / 事件操作分布 / 最近事件 /
Lamport 水位）。

