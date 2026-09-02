# Mímir 终极演进与架构深化实施规范 (v12.1.0 ~ v14.0)

> **面向外部开发 Agent (MiraSim / Claude Code)：**  
> 本文档是 Mímir 系统的终极演进规格说明书。请直接基于本文档中给出的架构设计、接口契约与分期任务进行代码编写与升级开发。

---

## 🏛️ 一、 理论体系与业界标杆融合架构

Mímir（当前运行基线 `v12.0.2` / Schema 18）深度融合了业界 6 大标杆记忆系统（**TencentDB-Agent-Memory、Graphiti、WikiSkill、XTMEM、Supermemory、Letta**）的核心工程精髓：

```
                                  【Mímir 终极全景架构】
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│  1. 接入与刚性闸门层 (XTMEM 四道闸门 · 顺序不可调)                                                │
│     ① Identity 认证 ──▶ ② Injection 注入拦截 ──▶ ③ Visibility 权限仲裁 ──▶ ④ Lineage 最严血缘强制  │
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│  2. 记忆与分层金字塔 (TencentDB 渐进式展开 & WikiSkill 演化)                                      │
│     • L0 Traces (不可变审计流) ──▶ L1 Atom Facts (原子事实) ──▶ L2 Patterns (Wiki) ──▶ L3 Skills│
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│  3. 协同与工作记忆区 (Letta / Blackboard)                                                       │
│     • Shared Working Memory / Task Scratchpad (多 Agent 协同排障秒级共享，任务后提炼或销毁)          │
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│  4. 检索与推理引擎 (Graphiti TKG 时态图谱 & 双层回落)                                             │
│     • Refined (结晶区) 优先命中 ──▶ 深度回落至 Canonical (全量底泥)                               │
│     • 时态图谱推理：每条关系边携带 valid_during: [t_start, t_end]，支持历史因果问答              │
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│  5. 异步生命周期引擎 (XTMEM 独立时钟驱动)                                                       │
│     • 衰减钟 (L0~L5 半衰期) • 结晶钟 (Auto-SOP) • 归名钟 (同义合并) • 冲突仲裁钟 • 梦境凝结钟       │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 📋 二、 分阶段演进规格与实施任务清单

### 阶段 1：v12.1.0（生产基线、标准化评测与全源摄入 · 当前实施）

#### 目标：
解耦硬编码，支持动态 Agent/Domain 扩展，建立自动化评测基准套件，打通全源摄入。

#### 核心任务与文件指引：
1. **动态 Agent/Domain 注册与白名单保护 (`mimir_v8/schema.py`, `mimir_v8/config.py`)**：
   - 移除静态硬编码 `AGENT_IDS` 和 `DOMAINS`，支持从 `mimir_config.yaml` 动态扩容。
   - **外置事实库保护**：支持挂载量化选股等外部独立事实库，升级时执行增量合并，严禁覆盖。
2. **Mímir-Eval 评测套件 (`mimir_v8/eval_suite.py`, `tests/test_p18_mimir_eval.py`)**：
   - 实现标准评测指标计算：`HitRate@K` (K=1,3,5,10)、`MRR`、`Extraction Precision`、`ACL 隔离泄漏率`。
   - 提供离线合成基准与在线金标（Golden Set）运行入口。
3. **全源自动化 CDC 采集管道 (`mimir_v8/collectors/`)**：
   - 统一 RSS、网页抓取与 Obsidian 双向同步调度至治理流水线。

---

### 阶段 2：v12.2.0（记忆分层、统一 Profile 视图与冲突闭环）

#### 目标：
落地 L0~L3 渐进式展开，提供一键画像解构端点，修复冲突事实投影器同步。

#### 核心任务与文件指引：
1. **L0~L3 记忆分层存储与渐进式展开 (`mimir_v8/crystallize.py`, `mimir_v8/store.py`)**：
   - `L0 Conversation`：原始对话与执行痕迹（证据层）；
   - `L1 Atom Facts`：原子事实与配置偏好；
   - `L2 Scenarios / Wiki`：高频排障模式与场景知识块；
   - `L3 Persona / Iron Rules`：核心人设与强约束铁律。
   - 检索时默认只装配 L3+L2，深度追溯才下钻 L1，大幅削减 Token 消耗。
2. **统一 Profile 视图 API (`/v12/profile` · `mimir_v8/api.py`)**：
   - 一键接口：传入 `agent_id`，直接返回解构后的 `{ "iron_rules": [...], "user_prefs": [...], "dynamic_context": [...] }`。
3. **血缘最严继承机制 (`mimir_v8/candidates.py`)**：
   - 派生事实与衍生知识的 `visibility` 强制继承来源中最严格的一档。
4. **冲突仲裁投影器闭环 (`mimir_v8/conflict.py`, `mimir_v8/projector.py`)**：
   - 修复 `status='disputed'` 事实在 FTS/Graph/Vector 中的同步逻辑，彻底消除 `mimir_v8_ops.py verify` 一致性门禁误报。

---

### 阶段 3：v13.0（多 Agent 协同工作黑板与时态知识图谱 TKG）

#### 目标：
从静态存取跃升至实时多 Agent 任务协同与跨时序因果推理。

#### 核心任务与文件指引：
1. **跨 Agent 共享工作记忆黑板 (`mimir_v8/blackboard.py`, `/v13/blackboard/*`)**：
   - 基于轻量 SQLite/内存的高并发瞬态工作区。
   - 多 Agent 协同排障时秒级读写临时状态，任务结束自动摘要沉淀至 Mímir 长期事实或安全销毁。
2. **时态知识图谱 (Temporal Context Graph · `mimir_v8/graph_projector.py`)**：
   - 在 `relations` 表中增加 `valid_from` 与 `valid_until` 时间区间。
   - 支持时序查询：`GET /v13/graph/history?entity_id=xxx&at_timestamp=yyy`。
3. **主动意图预测性前置唤醒 (`mimir_v8/relevance.py`)**：
   - Agent 接收到任务前置意图时，Mímir 主动推送历史避坑指南与 Iron Rules，无需被动发起搜索。

---

### 阶段 4：v14.0（WikiSkill 认知结晶、自动化技能与分布式联邦）

#### 目标：
吸收 WikiSkill 理论，实现经验自主编译为可复用技能，建立去中心化联邦。

#### 核心任务与文件指引：
1. **WikiSkill 技能自动编译流水线 (`mimir_v8/autoskill.py`)**：
   - 确立 **“Traces (L0) ──▶ Mímir Wiki (L1/L2) ──▶ Hermes Skills (L3)”** 三层演化链。
   - 胜任经验（成功解决 ≥3 次且反馈良好）自动提炼为标准 Hermes Skill，一键审批后全量挂载。
2. **跨节点去中心化加密联邦 (`mimir_v8/federation/`)**：
   - 支持多台家庭服务器（N100、台式机、云端节点）基于 CRDT 事件流加密同步。
3. **跨模型语义自适应投影**：
   - 适配不同模型窗口与输出格式，实现小模型挂载优质技能后的越级能力爆发。

---

## 🛠️ 三、 开发与测试规约

1. **工程四严律**：
   - **TDD 先行**：写代码前必须在 `tests/` 下编写对应测试并确认失败；
   - **不可变事件流**：所有状态变更必须先写 `memory_events`，严禁直接对主表进行破坏性修改；
   - **绝对路径安全**：所有配置与数据路径走 `MimirPaths` 统一管理；
   - **全量回归保障**：每次 commit 前运行 `python3 -m pytest`，确保全量测试通过。
