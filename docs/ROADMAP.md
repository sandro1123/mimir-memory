# Mímir Evolution Roadmap · 演进路线图

> **One shared memory, many agents.**  
> 本文档定义 Mímir 联邦记忆系统的全生命周期演进规划与技术路线（v12.0 ~ v14.0）。

---

## 🏛️ 理论体系与业界标杆融合架构 (Benchmark Fusion Architecture)

Mímir 深度融合业界 6 大标杆记忆系统的核心工程精髓（详见 [docs/plans/2026-09-02-mimir-comprehensive-evolution-spec-v12-to-v14.md](plans/2026-09-02-mimir-comprehensive-evolution-spec-v12-to-v14.md)）：

```
1. 接入与刚性闸门层 (XTMEM 四道闸门 · 顺序不可调)
   ① Identity 认证 ─▶ ② Injection 注入拦截 ─▶ ③ Visibility 权限仲裁 ─▶ ④ Lineage 最严血缘强制
2. 记忆与分层金字塔 (TencentDB 渐进式展开 & WikiSkill 演化)
   L0 Traces (不可变审计流) ─▶ L1 Atom Facts ─▶ L2 Patterns (Wiki) ─▶ L3 Skills
3. 协同与工作记忆区 (Letta / Blackboard) — 任务态秒级共享，事后提炼或销毁
4. 检索与推理引擎 (Graphiti TKG 时态图谱 & 双层回落) — Refined 结晶区优先，深度回落 Canonical
5. 异步生命周期引擎 (XTMEM 独立时钟) — 衰减钟 · 结晶钟 · 归名钟 · 冲突仲裁钟 · 梦境凝结钟
```

---

## 🗺️ 总体演进图景 (The Big Picture)

```
2026 Q3                          2026 Q4                          2027
┌─────────────────────────┐     ┌─────────────────────────┐     ┌─────────────────────────┐
│  v12.x: 基线、全源与分层  │ ──▶ │   v13.0: 协作与时序图谱  │ ──▶ │   v14.0: 认知结晶与去中心 │
│ (Base, Ingestion, Layer)│     │  (Working Memory & TKG) │     │  (Crystallization & P2P)│
├─────────────────────────┤     ├─────────────────────────┤     ├─────────────────────────┤
│ • Mímir-Eval 自动化评测  │     │ • 多Agent协同工作黑板    │     │ • 自动技能结晶 (AutoSOP) │
│ • RSS/Web/Vault 全源采集 │     │ • 时态知识图谱 (TKG)    │     │ • 跨节点去中心化联邦    │
│ • 动态 Agent/Domain 注册 │     │ • 主动预测性记忆唤醒    │     │ • 跨模型认知语义投影    │
│ • L0~L3 记忆分层与画像   │     │                         │     │                         │
└─────────────────────────┘     └─────────────────────────┘     └─────────────────────────┘
```

---

## 📌 阶段规划详解

### 1. v12.x 系列：基线收敛、全源采集与记忆分层 (2026 Q3)

**目标**：巩固事件溯源与联邦隔离底座，建立标准评测体系，打通全源自动化知识采集，落地记忆分层。

- [x] **v12.0.0 (Insight)**：艾宾浩斯 5 级衰减、Chronos 双时间轴、EvolveMem 反馈自进化、三通道召回漏斗（Vector + FTS + Graph）、冲突标记消解、MCP 27 工具支持。
- [x] **v12.0.2 (Security Hardening)**：多 Agent ACL 联邦隔离漏洞修复、特权逻辑彻底剥离、符号记忆租户隔离、SSRF 防护。
- [x] **v12.1.0 (Mímir-Eval & Ingestion)**（2026-08-31 发布，tag v12.1.0；部署前审计发现注册表未通电，热修 v12.1.1，2026-09-01）：
  - **Mímir-Eval 基准套件**：建立涵盖 `HitRate@K (K=1,3,5,10)`、`MRR`、`Extraction Precision` 与 `ACL Isolation Leak Rate` 的标准化自动化评测基准（09-02 spec 差距补齐：K=10 + 在线金标运行入口 + CLI `python -m mimir_v8.eval_suite --synthetic|--golden`，金标与回归测试共享单一事实源）。
  - **全源自动化采集管道**：将 RSS 订阅源、网页深度提取、Obsidian 笔记双向同步统一调度为 CDC 治理管道的输入源。
  - **动态 Agent 与 Domain 注册**：解耦 `schema.py` 中的硬编码 Agent 名单，支持通过 `mimir_config.yaml` 动态注册多智能体身份。
  - **开箱即用体验提升**：优化 Docker 容器端口编排，补充本地 BGE-M3 权重离线初始化脚本。
- [x] **v12.2.0 (Layered Memory, Unified Profile & XTMEM Gates)**（2026-09-03 发布，tag 候选 ad67148；详见 spec 阶段二，09-02 增补 XTMEM 锚通道与血缘最严继承；五件全零迁移，SCHEMA_VERSION 维持 19）：
  - **L0~L3 记忆分层存储与渐进式展开**（`query.py`，1caa538）：L0 原始对话与执行痕迹（证据层）→ L1 原子事实与配置偏好 → L2 高频排障模式与场景知识块（Wiki）→ L3 核心人设与强约束铁律；检索时默认只装配 L3+L2，深度追溯才下钻 L1，大幅削减 Token 消耗。
  - **统一 Profile 视图 API**（`/v12/profile` · `api.py`，1d9c1f1）：传入 `agent_id` 一键返回 `{iron_rules, user_prefs, dynamic_context}`。
  - **XTMEM 刚性闸门与血缘最严继承**（`candidates.py`，d274957）：派生事实的 `visibility` 强制继承来源中最严格一档；无来源一律按最严（`owner_only`）处理（Fail-Closed 原则）。
  - **检索锚通道 (Anchor Channel)**（`query.py`，4842e11）：高重要度铁律与用户核心偏好免于被语义相似度阈值一票否决，系统安全底线永不丢。
  - **冲突仲裁投影器闭环**（`conflict.py`，ef70a5c）：修复 `status='disputed'` 事实在 FTS/Graph/Vector 投影中的同步，消除 `verify` 一致性门禁误报。
  - 收尾两件：REST 层 `depth`/`use_anchor` 透传（8c5be69，`/v8/query` + `/v12/search/trace`）；版本 bump 12.1.4→12.2.0（ad67148，三锚定+CHANGELOG）。

---

### 2. v13.0 系列：多 Agent 协同工作黑板与时序认知 (2026 Q4)

**目标**：从“静态记忆存取”进化到“动态任务协同”，为多智能体群组提供实时共享工作区与时序因果推理。

- [x] **多 Agent 共享工作记忆 (Shared Working Memory / Blackboard)**（2026-09-03 收官，`blackboard.py` + `/v13/blackboard/*` REST 面）：
  - 在长期规范化存储之上，引入基于 SQLite 的共享工作黑板（Task Scratchpad）。
  - 支持多 Agent 在排查故障、量化分析或技术研讨时秒级共享局部任务上下文，任务结束后自动提炼总结为长期事实（distill）或安全销毁（destroy，留 audit 痕）。
  - 参与者边界硬闸：非参与者读写被拒；creator-not-participant 守卫自动回滚。
- [x] **时态知识图谱 (Temporal Knowledge Graph, TKG)**（2026-09-03 收官，a0aa913，SCHEMA_VERSION 19→20）：
  - 为关系边增加有效时间区间（`valid_from`/`valid_until`，空串=开放区间；supersede 写双向边：supersedes 开窗 + superseded_by 零宽关窗）。
  - 支持跨时间推理：`/v13/graph/history?at=ISO8601` 时点快照——"某时刻网络拓扑是什么状态？"、“某项配置在过去 30 天由哪个 Agent 进行了什么变更？”
  - 通用迁移链 `source_version <= 18` 恒 rebuild；专用 `migrate_schema_v19` 冻结产出真 19 形；守卫式 ALTER（`PRAGMA table_info` 先查列）免疫新库降 stamp 夹具。
- [x] **主动预测性召回 (Proactive & Predictive Recall)**（2026-09-03 收官，`relevance.py` + `/v13/wake`，65e35d3）：
  - Agent 在接收到任务指令前置阶段，Mímir 自动根据上下文意图与风险特征，主动推送历史避坑指南与强约束规则（Iron Rules），无需 Agent 被动发起查询。
  - IntentProfiler 关键词驱动意图分类（destructive > change > troubleshooting，轻实现可测不依赖 LLM）；铁律+核心偏好任何意图永远推送（安全底线）；L2 pattern 按意图家族匹配（排障意图推排障 pattern；generic 不推，宁缺毋滥）；全程 `can_read` ACL 仲裁（Fail-Closed）。

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

## 🛠️ 工程四严律 (Engineering Discipline)

所有 v12.1.0 ~ v14.0 范围内的开发遵循（spec 第三节钉死）：

1. **TDD 先行**：写实现前必须在 `tests/` 下编写对应测试并确认失败（RED → GREEN）。
2. **不可变事件流**：所有状态变更必须先写 `memory_events`，严禁直接对主表进行破坏性修改。
3. **绝对路径安全**：所有配置与数据路径走 `MimirPaths` 统一管理。
4. **全量回归保障**：每次 commit 前运行 `python3 -m pytest`，确保全量测试通过。

---

## 🤝 贡献与参与

我们欢迎社区共同参与 Mímir 的路线图实现：
- 提出架构建议：欢迎提交 [RFC / Discussion](https://github.com/sandro1123/mimir-memory/discussions)
- 提交 Bug 或需求：参见 [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE/)
- 安全漏洞披露：请遵照 [SECURITY.md](SECURITY.md) 流程
