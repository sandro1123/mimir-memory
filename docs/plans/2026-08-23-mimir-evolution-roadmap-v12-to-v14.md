# Mímir 演进与系统升级规划全景方案 (v12.1.0 ~ v14.0)

> **核心愿景**：从“静态检索外脑”进化为“多 Agent 实时协同、时序认知与自动技能结晶的分布式联邦记忆中枢”。

---

## 🏛️ 业界前沿框架吸收与借鉴矩阵

结合对 **TencentDB-Agent-Memory**、**Graphiti (Zep)**、**Mem0**、**Cognee**、**Supermemory** 及 **Letta** 官方代码库与核心架构的深度调研，Mímir 将吸收以下核心设计精华：

| 标杆项目 | 核心架构特色 | Mímir 架构借鉴与吸收点 |
| :--- | :--- | :--- |
| **TencentDB Agent Memory** | **L0~L3 语义金字塔 + 符号化短时记忆**<br>• L0(Conversation) → L1(Atom) → L2(Scenario) → L3(Persona)<br>• 工具日志压缩为 Mermaid 符号图，Token 降 60%+ | **1. 四层记忆渐进式展开（Progressive Disclosure）**<br>底层数据库保存证据（SQLite），顶层结构化 Markdown 注入 Prompt，避免无脑平铺检索。<br>**2. 工具执行轨迹符号化**：大段工具输出提炼为轻量状态图。 |
| **Graphiti (Zep)** | **时态上下文图谱 (Temporal Context Graph)**<br>• 事实带有时效窗口 `valid_during: [t_start, t_end]`<br>• 显式双时间轴（Bi-temporal），旧事实标记失效而非硬删除 | **1. 时序关系图谱推理**：在现有 Graph Projector 基础上增加时间有效区间，支持“过去何时为真、现在是否为真”的时序因果查询。<br>**2. 冲突自动时序失效**：新事实自动淘汰老关系并保留版本回溯。 |
| **Supermemory** | **静态/动态画像统一解构 (`profile.static` / `dynamic`)**<br>一次调用解构强约束画像与当前任务上下文 | **1. 统一 Profile 视图接口 (`/v12/profile`)**<br>一键输出 Agent 自身的 `iron_rules`（强约束铁律）+ `user_prefs`（稳定偏好）+ `working_context`（动态工作区），减少 Prompt 装配开销。 |
| **Mem0 & Cognee** | **实体关联图谱 + 轻量内嵌式存储架构**<br>• 混合检索（向量 + BM25 + 实体链接）<br>• 纯嵌入式部署（SQLite + LanceDB） | **1. 坚持轻量化嵌入式设计**：保持纯 Python + SQLite3 + ChromaDB 本地运行，极度适配家庭服务器 N100。<br>**2. Skills 工具包标准化**：为 OpenCode 和 Hermes 提供标准 MCP / Tool 调用库。 |
| **Letta (MemGPT)** | **Agent 运行时自适应记忆管理 (Harness & Working Memory)** | **1. 跨 Agent 共享工作记忆黑板 (Blackboard)**：多 Agent 协同工作时的临时瞬态状态共享。 |

---

## 🗺️ Mímir 版本升级路线图 (v12.1.0 ~ v14.0)

```
2026 Q3 (近期)                     2026 Q4 (中期)                     2027 (远期)
┌───────────────────────────┐     ┌───────────────────────────┐     ┌───────────────────────────┐
│   v12.1.0 ~ v12.2.0       │ ──▶ │   v13.0: 协作与时态图谱   │ ──▶ │   v14.0: 技能结晶与分布式 │
│ (生产基线、评测与全源摄入) │     │ (Working Memory & TKG)    │     │ (AutoSOP & P2P Federation)│
├───────────────────────────┤     ├───────────────────────────┤     ├───────────────────────────┤
│ • 动态 Agent/Domain 注册  │     │ • 跨Agent共享工作黑板     │     │ • 自动技能结晶 (Auto-Skill)│
│ • Mímir-Eval 标准评测套件 │     │ • 时态知识图谱 (Temporal) │     │ • 跨节点去中心化加密同步  │
│ • 全源自动化 CDC 采集管道 │     │ • 统一 Profile 视图 API   │     │ • 跨模型自适应语义重构    │
│ • 智能冲突消解与投影同步  │     │ • 主动预测性前置唤醒      │     │ • 社区化共识与治理审计    │
└───────────────────────────┘     └───────────────────────────┘     └───────────────────────────┘
```

---

## 📋 各版本详细规划与落地规格

### 阶段一：v12.1.0（生产基线、评测与全源摄入 · 当前进行中）
* **目标**：彻底解耦硬编码，打通自动化评测基准与全源输入。
1. **动态 Agent 与 Domain 注册与白名单保护机制**：
   - 彻底解耦 `schema.py`，支持在 `mimir_config.yaml` 动态注册多 Agent（如新建分析师、运维子 Agent）与业务领域。
   - **外置/单建事实库保护策略**：支持量化选股等外部项目独立单建事实库的挂载与合并迁移通道，升级时自动合并保留白名单与外部事实，严禁直接覆盖。
2. **Mímir-Eval 自动化评测套件**：
   - 建立涵盖 `HitRate@1/3/5/10`、`MRR`、`Extraction Precision` 与 `ACL 隔离泄漏率` 的标准评测。
3. **全源自动化 CDC 采集管道**：
   - 将 RSS 资讯、Web 网页提取、Obsidian Vault 笔记双向同步统一调度为治理流。

### 阶段二：v12.2.0（记忆分层、Profile 解构与冲突修复 · 近期规划）
* **目标**：吸收 TencentDB 与 Supermemory 的分层理念，优化检索体验与系统稳健性。
1. **L0~L3 记忆分层与渐进式展开（Progressive Disclosure）**：
   - 划分 L0（原始对话 trace）→ L1（原子事实）→ L2（场景/项目块）→ L3（核心画像/规则）。
   - 正常 Prompt 默认只装配 L3+L2，遇到深入细节才下钻 L1，大幅减少 Token 消耗。
2. **统一 Profile 视图端点 (`/v12/profile`)**：
   - 提供专用端点，一次查询直接返回 `static_rules`（铁律/偏好）与 `dynamic_context`（动态状态）。
3. **争议/冲突裁决的投影器闭环同步**：
   - 彻底修复 `disputed` 状态在 FTS/Graph/Vector 中的同步逻辑，消除一致性巡检门禁误报。

### 阶段三：v13.0（多 Agent 协同工作黑板与时态图谱 · 中期演进）
* **目标**：从“静态存取”跃升为“动态协同与时序因果”。
1. **跨 Agent 共享工作记忆 (Shared Working Memory / Blackboard)**：
   - 在长期不可变存储之上，开辟基于轻量 SQLite/内存的共享工作黑板。
   - Heimdallr、QuantMaster、JARVIS、Mentor 协同排障或分析时秒级共享局部上下文，任务结束后自动提炼总结为长期事实或安全销毁。
2. **时态知识图谱 (Temporal Knowledge Graph, TKG)**：
   - 借鉴 Graphiti，为关系边引入 `valid_from` 与 `valid_until` 时间区间。
   - 支持时序因果问答：“某服务器配置在 8 月 10 日是什么状态？何时被谁修改成了现在这样？”
3. **主动意图预测性唤醒 (Proactive Recall)**：
   - Agent 接收到任务前置意图时，Mímir 自动主动推送强约束（Iron Rules）与避坑指南，变“被动查”为“主动护航”。

### 阶段四：v14.0（认知结晶、自动化技能与分布式联邦 · 远期愿景）
* **目标**：实现集体经验自主进化与跨设备去中心化生态。
1. **技能自动结晶流水线 (Auto Skill Crystallization)**：
   - 自动检测多 Agent 协作中的高频优质排障链路（解决次数 ≥3 且反馈率 ≥90%）。
   - 自动提炼为标准 Hermes / OpenCode Skill SOP，人类一键审核后直接挂载。
2. **跨节点去中心化加密联邦 (Edge-to-Edge Decentralized Federation)**：
   - 支持多台家庭服务器（N100、台式机、云端节点）基于 CRDT 事件流加密同步，实现高可用容灾。
3. **跨模型语义自适应投影**：
   - 自动适配 Claude、DeepSeek、Gemini、本地开源模型的不同上下文窗口与格式特性。
