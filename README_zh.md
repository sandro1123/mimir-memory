# Mímir — 面向多智能体系统的联邦记忆

> **一份共享记忆，多个智能体。** 一个事件溯源、自我演化、联邦化的记忆系统，
> 让多个 AI 智能体「一起」记忆——并智能地遗忘。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-18-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README.md) · [中文](README_zh.md)

---

## Mímir 独一无二的能力：**联邦记忆**

大多数记忆系统为**单个智能体**设计。Mímir 为**多个智能体**设计。

在多智能体系统中——网络运维智能体、量化投顾智能体、技术顾问、培训师——每个
智能体职责不同、知识不同、归属不同。它们不该看到所有内容，但**应该**能共享
真正重要的东西。

Mímir 的答案是**细粒度隔离的联邦记忆**：

- **每个智能体有自己的记忆**——事实用 `owner_principal` 打标签，ACL 精确控制
  谁能读什么。
- **智能体有意识地共享**——三档可见性（`all` / `shared` / `owner_only`）让你把
  事实标记为「仅我自己」「我的团队」或「所有智能体可见」。
- **跨智能体感知**——感知广播呈现其他智能体最近学到了什么，让智能体不再各自为战。
- **联邦搜索**——`/v10/federation/{peer}` 在 ACL 约束下跨主体查询，让一个智能体
  能安全地问「有没有谁知道关于 X 的事？」。

结果：**N 个智能体共享一份记忆底座，同时保有 N 份私有记忆的隔离性。** 这就是
「记忆存储」与「集体记忆」的区别。

---

## Mímir 的其他与众不同之处（联邦之外）

联邦记忆是招牌。但 Mímir 也建立在一个与「带 API 的向量数据库」根本不同的前提上：
**记忆是事件，不是一行数据。**

| 普通的记忆存储 | Mímir |
|---|---|
| 为单个智能体设计 | **为 N 个智能体设计，ACL 隔离的联邦** |
| 覆盖旧记忆 | **追加不可变事件**——历史永不改写 |
| 「遗忘」= 删行 | **墓碑遗忘**——标记，而非销毁 |
| 记忆质量靠你的 prompt | **可治理**——LLM *评估*每个候选；只能*建议*，不能*提交* |
| 静态检索分 | **自我演化**——反馈让置信度可升可降 |
| 单一向量索引 | **三通道融合**——向量 + 全文 + 图，RRF + 本地重排 |
| 事实随意衰减 | **艾宾浩斯衰减**——五条遗忘曲线，从永不遗忘到临时 |

Mímir 是集体智能体记忆的完整生命周期：
*摄入 → 治理 → 提交 → 检索 → 自我纠偏 → 遗忘*——每一步可审计、可回滚。

---

## 一图看懂核心架构

```
                         ┌─────────────────────────────┐
   对话 / 采集 ────────▶ │          治理层             │
                         │  候选 → LLM 评估 →          │
                         │  噪声 / 暂定 / 人工审核 /    │
                         │  提交                     │
                         └──────────────┬──────────────┘
                                        ▼
                         ┌─────────────────────────────┐
                         │  规范化存储（事件溯源）      │
                         │  facts + memory_events +     │
                         │  fact_versions（不可变）      │
                         │  owner_principal + ACL       │
                         └──────────────┬──────────────┘
                                        ▼  （outbox 扇出）
              ┌──────────────┬──────────┴──────────┬──────────────┐
              ▼              ▼                     ▼              ▼
          向量 (chroma)   全文 (FTS5)           图             核心记忆
              └──────────────┴──────────┬──────────┴──────────────┘
                                        ▼
                              RRF 融合 + 本地重排
                                        ▼
                          排序后、经 ACL 过滤的结果
                          （逐智能体可见性强制）
```

---

## 六大支柱

### 1. 联邦记忆（多 Agent 联邦记忆）— *招牌能力*
多个智能体共享一份记忆底座，`owner_principal` 隔离、三档可见性、跨智能体感知、
带 ACL 的联邦搜索。多智能体接入指南见 [docs/FEDERATION.md](docs/FEDERATION.md)。

### 2. 事件溯源真相（事件溯源）
每条事实都是追加式事件流。`memory_events` 和 `fact_versions` 受触发器保护，
拒绝 UPDATE 和 DELETE——可回放、可审计、可解释，是结构属性。

### 3. 治理闭环（受控的摄入）
确定性规则引擎 + 独立的 LLM 评估器在提交前分类每个候选。LLM 被**刻意与提交路径
分离**——不能既提取又批准。

### 4. 对称自我演化（检索自进化）
检索反馈（`有用`/`无用`/`纠正`）按 7 天窗口聚合，置信度可升可降，受最小信号数
门槛约束。

### 5. 科学的遗忘（艾宾浩斯曲线）
五条艾宾浩斯衰减层级 + Chronos 双时间轴。身份规则永不衰减，临时事实 7 天半衰期，
过期事实被降权——永不删除。

### 6. 本地优先隐私（本地隐私）
所有嵌入（bge-m3）和重排（ms-marco）在**本地 CPU** 运行——待嵌入文本绝不出机器。
API 仅绑定 `127.0.0.1`。

---

## 快速开始（单智能体）

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory
pip install -e ".[embeddings]"

export MIMIR_HOME=~/.hermes/mimir
export MIMIR_DATA_DIR=$MIMIR_HOME/data
export MIMIR_SECRETS_DIR=$MIMIR_HOME/secrets

python -m mimir_v8.server --bind 127.0.0.1 --port 8456
```

> **多智能体联邦接入**，请运行 `./scripts/init.sh` 引导生成智能体配置与 token，
> 然后按 [docs/FEDERATION.md](docs/FEDERATION.md) 操作。

---

## 能力矩阵

| 能力 | Mímir |
|---|---|
| **多智能体联邦记忆 + ACL 隔离** | ✅ |
| 跨智能体感知广播 | ✅ |
| 联邦跨主体搜索 | ✅ |
| 事件溯源（不可变事件）| ✅ |
| 治理管线（LLM 评估器）| ✅ |
| 向量 + 全文 + 图融合（RRF）| ✅ |
| 本地 CPU 嵌入与重排 | ✅ |
| 检索反馈自我演化 | ✅ |
| 艾宾浩斯衰减 + Chronos 双时间轴 | ✅ |
| 冲突消解（标记争议，永不删除）| ✅ |
| 技能结晶 | ✅ |
| 多模态事实资产 | ✅ |
| Obsidian wikilink 双向链接 | ✅ |
| MCP 服务（27 工具）| ✅ |
| Hermes MemoryProvider 插件 | ✅ |
| Dashboard（9 标签页 Web 界面）| ✅ |
| PyPI + Docker 打包 | ✅ |

---

## 路线图

| 里程碑 | 范围 | 状态 |
|---|---|---|
| v10.0 | 包内治理、Opinion/Observation 置信度层 | ✅ 已发布 |
| v11.0 | 符号短时记忆 + CodeGraph + reflect/federation API | ✅ 已发布 |
| v12.0 | Insight：艾宾浩斯衰减、Chronos、EvolveMem、召回漏斗、冲突消解、技能结晶、MCP、多模态、PyPI/Docker | ✅ 已发布 |
| v12+ | Hermes MemoryProvider 集成、检索评测基线 | 🔵 进行中 |

---

## 安全

- API 仅绑定 `127.0.0.1`；远程访问经反向代理（nginx / Cloudflare Tunnel）
- 每个端点 Bearer token 鉴权 + 权限范围（read/write/review/manage/admin）
- SQLite 触发器保证 `memory_events` / `fact_versions` 不可变
- 所有变更携带幂等键 + `actor_principal` + 审计日志
- `egress_policy=local_only` 阻止敏感事实被外部处理

漏洞披露政策见 [SECURITY.md](SECURITY.md)。

---

## 鸣谢

Mímir 站在多个优秀开源记忆项目的肩膀上。我们衷心感谢它们的作者：

| 项目 | 作者 | 我们学到了什么 |
|---|---|---|
| [aiduMEI](https://github.com/monkey2jack/aiduMEI) | [monkey2jack](https://github.com/monkey2jack) | 塑造 Mímir v12「Insight」的**治理 + 自我演化愿景**：Tahoe-Gate 相关性门控、EvolveMem 反馈回路、冲突消解、技能结晶。对我们设计影响最大的单一项目。 |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | 腾讯云 | 符号短时记忆（Mermaid 画布卸载 + 下钻）与 CodeGraph 索引 |
| [Hindsight](https://github.com/obsidianforensics/hindsight) | Obsidian Forensics | 信念建模——区分「我知道什么」与「我有多大把握」的 Opinion/Observation 层 |
| [Mem0](https://github.com/mem0ai/mem0) / [MemGPT](https://github.com/cpacker/MemGPT) | mem0ai / cpacker | 记忆管线范式：分层存储、上下文管理、记忆作为一等公民服务 |

**特别致意 [aiduMEI](https://github.com/monkey2jack/aiduMEI)**（aidu Memory
Engine Insight，「爱嘟优忆思」）：除了上述四个借鉴模式，其作者关于**原文保真 vs
蒸馏**的深刻思考——「蒸馏会丢温度，原文才是证据」——直接启发了 Mímir 的保留豁免
设计：被已提交事实引用的对话消息永不被清理。我们诚心推荐你去了解 aiduMEI。

---

## 许可证

[MIT](LICENSE)

## 联系方式

维护者：**sandro1123** · 📧 [sandro1123@hotmail.com](mailto:sandro1123@hotmail.com)
