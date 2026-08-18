# Mímir — 懂得「如何遗忘」的记忆系统

> 为 AI 智能体打造的、事件溯源、自我演化、联邦化的记忆系统。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Schema Version](https://img.shields.io/badge/schema-18-blue.svg)](#)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg)](#)
[![CI](https://github.com/sandro1123/mimir-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/sandro1123/mimir-memory/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[English](README.md) · [中文](README_zh.md)

---

## Mímir 为什么与众不同

大多数记忆系统本质上是「**带 API 的数据库**」——存向量、返回最接近的匹配。
Mímir 建立在完全不同的前提上：**记忆是事件，不是一行数据。**

这一个决定改变了后续的一切：

| 普通的记忆存储 | Mímir |
|---|---|
| 覆盖旧记忆 | **追加不可变事件**——每次变更都是新事件，历史永不改写 |
| 「遗忘」= 删行 | **墓碑遗忘**——遗忘但不删除；事实被标记，而非被销毁 |
| 记忆质量靠你的 prompt | **可治理**——LLM 在提交前*评估*每个候选；LLM 只能*建议*，不能*提交* |
| 静态检索分 | **自我演化**——检索反馈（有用/无用/纠正）随时间微调置信度 |
| 单一向量索引 | **三通道融合**——向量 + 全文 + 图，RRF 融合 + 本地重排 |
| 事实随意衰减 | **艾宾浩斯衰减**——五条遗忘曲线，从永不遗忘到 7 天半衰期 |

Mímir 不是「又一个 RAG 层」。它是智能体记忆的**完整生命周期**：
*摄入 → 治理 → 提交 → 检索 → 自我纠偏 → 遗忘*——每一步都可审计、可回滚。

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
```

---

## 六大独特优势

### 1. 事件溯源真相（不可篡改的账本）
每条事实都是追加式事件流。`memory_events` 和 `fact_versions` 受 SQLite
触发器保护——**拒绝 UPDATE 和 DELETE**。你可以回放、审计、解释「为什么
这条记忆是这样的」——这是结构属性，不是口头承诺。

### 2. 治理闭环（受控的摄入）
任何候选在成为事实之前，都要经过治理管线：确定性规则引擎 + 独立的 LLM
评估器，将其分类为噪声、低风险或不确定。LLM 被**刻意与提交路径分离**——
同一个模型不能既提取又批准。每个决策都写入 `audit_log`。

### 3. 对称自我演化（检索自进化）
检索反馈（`有用` / `无用` / `纠正`）按 7 天窗口聚合，微调事实置信度——
**可升可降**，并受最小信号数门槛约束，两次幸运命中无法虚增权重。记忆
用得越多越可信，失效时则自动降权。

### 4. 科学的遗忘（艾宾浩斯曲线）
五条遗忘层级基于艾宾浩斯曲线建模，外加 Chronos 双时间轴（`valid_from` /
`valid_to`）：身份级规则永不衰减，临时事实 7 天半衰期，过期事实被降权——
**但永不删除**。

### 5. 三层知识（记忆/学习/文档）
记忆不是一堆扁平数据。Mímir 分离 **memory**（事实）、**learning**（方法/
经验）、**wiki**（文档），各自拥有独立的生命周期、授权与反馈回路。技能结晶
自动把反复出现的主题聚类为可复用的 pattern 事实——人始终在回路中。

### 6. 联邦隔离 + 默认本地隐私
通过 `owner_principal` + ACL 实现多智能体隔离。所有嵌入（bge-m3）和重排
（ms-marco）**在本地 CPU 上运行**——待嵌入的文本绝不出你的机器。API 仅绑定
`127.0.0.1`。

---

## 快速开始

```bash
git clone git@github.com:sandro1123/mimir-memory.git
cd mimir-memory
pip install -e ".[embeddings]"        # 包含本地 bge-m3 嵌入

# 在 MIMIR_HOME/secrets 下创建自己的密钥
export MIMIR_HOME=~/.hermes/mimir
export MIMIR_DATA_DIR=$MIMIR_HOME/data
export MIMIR_SECRETS_DIR=$MIMIR_HOME/secrets

python -m mimir_v8.server --bind 127.0.0.1 --port 8456
```

随后访问 `/health` 确认，完整的上手指南见 [`examples/QUICKSTART.md`](examples/QUICKSTART.md)。

---

## 能力矩阵

| 能力 | Mímir |
|---|---|
| 事件溯源（不可变事件）| ✅ |
| 治理管线（LLM 评估器）| ✅ |
| 多智能体联邦 + ACL | ✅ |
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

Mímir 站在多个优秀开源记忆项目的肩膀上。我们衷心感谢它们的作者，感谢
那些被我们借鉴并深化的思想：

| 项目 | 作者 | 我们学到了什么 |
|---|---|---|
| [aiduMEI](https://github.com/monkey2jack/aiduMEI) | [monkey2jack](https://github.com/monkey2jack) | 塑造 Mímir v12「Insight」的**治理 + 自我演化愿景**：Tahoe-Gate 相关性门控、EvolveMem 反馈回路、冲突消解、技能结晶。这是对我们设计影响最大的单一项目。 |
| [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | 腾讯云 | 符号短时记忆（Mermaid 画布卸载 + 下钻）与 CodeGraph 索引 |
| [Hindsight](https://github.com/obsidianforensics/hindsight) | Obsidian Forensics | 信念建模——区分「我知道什么」与「我有多大把握」的 Opinion/Observation 层 |
| [Mem0](https://github.com/mem0ai/mem0) / [MemGPT](https://github.com/cpacker/MemGPT) | mem0ai / cpacker | 记忆管线范式：分层存储、上下文管理、记忆作为一等公民服务 |

**特别致意 [aiduMEI](https://github.com/monkey2jack/aiduMEI)**（aidu Memory
Engine Insight，「爱嘟优忆思」）：除了上述四个借鉴模式，其作者关于
**原文保真 vs 蒸馏**的深刻思考——「蒸馏会丢温度，原文才是证据」——直接启发了
Mímir 的保留豁免设计：被已提交事实引用的对话消息永不被清理。我们怀着同样的
精神在构建，也诚心推荐你去了解 aiduMEI。

---

## 许可证

[MIT](LICENSE)
