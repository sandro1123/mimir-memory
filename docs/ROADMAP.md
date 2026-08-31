# Mímir Evolution Roadmap · 演进路线图

> **One shared memory, many agents.**  
> 本文档定义 Mímir 联邦记忆系统的全生命周期演进规划与技术路线（v12.0 ~ v14.0）。

---

## 🗺️ 总体演进图景 (The Big Picture)

```
2026 Q3                          2026 Q4                          2027
┌─────────────────────────┐     ┌─────────────────────────┐     ┌─────────────────────────┐
│   v12.x: 生产基线与全源   │ ──▶ │   v13.0: 协作与时序图谱  │ ──▶ │   v14.0: 认知结晶与去中心 │
│  (Base & Ingestion)     │     │  (Working Memory & TKG) │     │  (Crystallization & P2P)│
├─────────────────────────┤     ├─────────────────────────┤     ├─────────────────────────┤
│ • Mímir-Eval 自动化评测  │     │ • 多Agent协同工作黑板    │     │ • 自动技能结晶 (AutoSOP) │
│ • RSS/Web/Vault 全源采集 │     │ • 时态知识图谱 (TKG)    │     │ • 跨节点去中心化联邦    │
│ • 动态 Agent/Domain 注册 │     │ • 主动预测性记忆唤醒    │     │ • 跨模型认知语义投影    │
└─────────────────────────┘     └─────────────────────────┘     └─────────────────────────┘
```

---

## 📌 阶段规划详解

### 1. v12.x 系列：基线收敛与全源采集 (2026 Q3)

**目标**：巩固事件溯源与联邦隔离底座，建立标准评测体系，打通全源自动化知识采集。

- [x] **v12.0.0 (Insight)**：艾宾浩斯 5 级衰减、Chronos 双时间轴、EvolveMem 反馈自进化、三通道召回漏斗（Vector + FTS + Graph）、冲突标记消解、MCP 27 工具支持。
- [x] **v12.0.2 (Security Hardening)**：多 Agent ACL 联邦隔离漏洞修复、特权逻辑彻底剥离、符号记忆租户隔离、SSRF 防护。
- [x] **v12.1.0 (Mímir-Eval & Ingestion)**（2026-08-31 发布，tag v12.1.0；部署前审计发现注册表未通电，热修 v12.1.1，2026-09-01）：
  - **Mímir-Eval 基准套件**：建立涵盖 `HitRate@5`、`MRR`、`Extraction Precision` 与 `ACL Isolation Leak Rate` 的标准化自动化评测基准。
  - **全源自动化采集管道**：将 RSS 订阅源、网页深度提取、Obsidian 笔记双向同步统一调度为 CDC 治理管道的输入源。
  - **动态 Agent 与 Domain 注册**：解耦 `schema.py` 中的硬编码 Agent 名单，支持通过 `mimir_config.yaml` 动态注册多智能体身份。
  - **开箱即用体验提升**：优化 Docker 容器端口编排，补充本地 BGE-M3 权重离线初始化脚本。

---

### 2. v13.0 系列：多 Agent 协同工作黑板与时序认知 (2026 Q4)

**目标**：从“静态记忆存取”进化到“动态任务协同”，为多智能体群组提供实时共享工作区与时序因果推理。

- [ ] **多 Agent 共享工作记忆 (Shared Working Memory / Blackboard)**：
  - 在长期规范化存储之上，引入基于轻量内存/SQLite 的共享工作黑板（Task Scratchpad）。
  - 支持多 Agent 在排查故障、量化分析或技术研讨时秒级共享局部任务上下文，任务结束后自动提炼总结为长期事实或安全销毁。
- [ ] **时态知识图谱 (Temporal Knowledge Graph, TKG)**：
  - 为实体与关系边增加有效时间区间（`valid_during: [t_start, t_end]`）。
  - 支持跨时间推理：“某时刻网络拓扑是什么状态？”、“某项配置在过去 30 天由哪个 Agent 进行了什么变更？”
- [ ] **主动预测性召回 (Proactive & Predictive Recall)**：
  - Agent 在接收到任务指令前置阶段，Mímir 自动根据上下文意图与风险特征，主动推送历史避坑指南与强约束规则（Iron Rules），无需 Agent 被动发起查询。

---

### 3. v14.0 系列：认知结晶与去中心化联邦 (2027)

**目标**：实现集体经验的自主进化，从记忆积累跃升为技能结晶，支持跨节点分布式联邦。

- [ ] **技能自动结晶流水线 (Auto Skill Crystallization)**：
  - 自动检测多 Agent 协作中的高频优质排障链路（解决次数 ≥3 且有用反馈率 ≥90%）。
  - LLM 自动将该排障经验提炼为标准 Hermes Skill SOP，经人类一键审批（Human-in-the-loop）后直接挂载至 Agent 技能库。
- [ ] **边缘多节点联邦同步 (Edge-to-Edge Decentralized Federation)**：
  - 支持跨多台家庭服务器、云端边缘实例的 Mímir 节点进行加密同步与 ACL 鉴权共享。
  - 采用 CRDT（无冲突复制数据类型）或去中心化事件日志机制，保障离线容灾与最终一致性。
- [ ] **跨模型认知语义投影 (Cross-Model Cognitive Alignment)**：
  - 适配不同基础模型（Claude / DeepSeek / Gemini / 本地小模型）的记忆上下文注入格式，实现记忆形态的自适应压缩与重构。

---

## 📊 版本特性对比矩阵

| 能力维度 | v11 (Symbolic) | v12 (Insight / Current) | v13 (Collaboration) | v14 (Crystallization) |
|:---|:---:|:---:|:---:|:---:|
| **多 Agent 隔离与共享** | 静态 ACL | 细粒度三档 ACL + 跨 Agent 感知 | 实时协同工作黑板 (Blackboard) | 跨节点分布式联邦 |
| **存储范式** | SQLite 规范化 | 事件溯源不可变流 (Event-Sourced) | 事件溯源 + 瞬态工作区 | 分布式 CRDT 事件流 |
| **检索模式** | 向量 + FTS | 向量 + FTS + Graph (RRF 融合) | 三通道融合 + 时态图谱推理 | 意图主动预测召回 |
| **自进化机制** | 基础置信度 | EvolveMem (7天反馈动态调节) | 动态冲突裁决 + 观察聚合 | 经验自主提炼生成 Skill |
| **治理机制** | 规则审核 | 规则 + LLM 独立评估分离 | 上下文自适应流式治理 | 社区化共识与治理审计 |

---

## 🤝 贡献与参与

我们欢迎社区共同参与 Mímir 的路线图实现：
- 提出架构建议：欢迎提交 [RFC / Discussion](https://github.com/sandro1123/mimir-memory/discussions)
- 提交 Bug 或需求：参见 [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE/)
- 安全漏洞披露：请遵照 [SECURITY.md](SECURITY.md) 流程
