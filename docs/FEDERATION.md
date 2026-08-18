# Multi-Agent Federation Guide · 多智能体联邦接入指南

> English · 中文双语

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
