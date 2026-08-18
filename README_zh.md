# Mímir — 联邦记忆系统

> 为 AI 助手构建的持久化、可治理、可查询的记忆系统

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-18-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)

---

## 什么是 Mímir

Mímir 是一个**联邦化、事件溯源（event-sourced）的记忆系统**，专为 AI 智能体（Agent）
与知识工作者设计。与把记忆当作向量嵌入的向量数据库不同，Mímir 把记忆视为**不可变的事件**，
具备显式的生命周期、治理（governance）与多层查询能力。

### 核心原则

- **事件溯源**：每条事实都是追加式（append-only）事件流，受 ACID 触发器保护
- **可治理**：LLM 辅助分类、人工审核、审计日志、逐事实 ACL
- **可查询**：向量 + 全文 + 图 三路融合，通过 RRF 得到排序结果
- **可持久**：SQLite 规范化存储，`memory_events` 与 `fact_versions` 不可变

---

## 快速开始

### 环境

```bash
# 克隆
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory

# 安装依赖
pip install fastapi uvicorn httpx jinja2 aiofiles chromadb sentence-transformers

# 环境变量（在 $MIMIR_HOME/secrets 下创建自己的密钥）
export MIMIR_HOME=~/.hermes/mimir
export MIMIR_DATA_DIR=$MIMIR_HOME/data
export MIMIR_SECRETS_DIR=$MIMIR_HOME/secrets
export MIMIR_CONFIG_FILE=$MIMIR_HOME/mimir_config.yaml

# 仅监听回环地址启动
python -m mimir_v8.server --bind 127.0.0.1 --port 8456
```

更多细节见 [examples/QUICKSTART.md](examples/QUICKSTART.md)。

---

## 核心概念（30 秒速览）

| 概念 | 含义 |
|---|---|
| **事件溯源** | 每次变更都是 `memory_events` 的追加行，永不覆盖 |
| **三层结构** | `memory`（事实）、`learning`（方法）、`wiki`（文档）|
| **可治理** | LLM + 确定性策略在提交前对每个候选分类 |
| **查询融合** | 向量 + 全文 + 图 通过 RRF 融合 |
| **自我演化** | 检索反馈随时间调整事实置信度（EvolveMem）|
| **墓碑遗忘** | 遗忘不删除，标记 `tombstoned` 并从活跃检索隐藏 |

---

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md)（完整设计）与 [docs/MIMIR-v12-GOAL.md](docs/MIMIR-v12-GOAL.md)（v12 路线图）。

---

## 安全

- API 仅绑定 `127.0.0.1`；外部访问需经 nginx / Cloudflare Tunnel 反向代理
- 每个端点使用 Bearer token + 权限范围（read/write/review/manage/admin）
- SQLite 触发器防止 `memory_events` / `fact_versions` 被 UPDATE/DELETE
- 所有变更携带幂等键 + actor_principal + 审计日志
- 敏感配置可设 `egress_policy=local_only` 阻止外部处理

---

## 路线图

| 里程碑 | 范围 | 状态 |
|---|---|---|
| v10.0 | 治理主流程、Opinion/Observation 层 | ✅ 已发布 |
| v11.0 | 符号短时记忆 + CodeGraph | ✅ 已发布 |
| v12.0 | Ebbinghaus 遗忘、Chronos、EvolveMem、冲突消解、技能结晶、MCP、多模态 | ✅ 已发布 |
| v12+ | Hermes MemoryProvider 集成、检索评测基线 | 🔵 进行中 |

---

## 贡献

欢迎提交 [Issue](https://github.com/sandro1123/mimir-memory/issues) 与
[Pull Request](https://github.com/sandro1123/mimir-memory/pulls)。请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)
