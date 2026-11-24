# Mímir P0 真值盘点报告（1.9.x → 2.x）

> **日期**：2026-11-19  
> **执行**：Thor（agent）对 `2026-11-19-mimir-1.9-to-2x-development-path.md` 与 `work/mimir`（当前 `1.7.0`、`SCHEMA_VERSION=21`）的实地核查  
> **方法**：代码静态检查 + 关键模块读取 + 路径文档逐条对照 + Mirasim 本地实况（`~/.mirasim/`）取事实  
> **目的**：消除排期幻觉，确定 1.9.0 范围，锁定 P1/P2 先决条件

---

## 1. 核心判断

**路线图的主题划分与护栏（治理不稀释、诚实判语、先量后写、fail-closed、声明式适配）是正确的**，符合 Mímir “可信底座的记忆系统 + Agent 知识库” 定位。**但它更像版本主题路线图，不足以直接作为可控交付的工程与治理方案。**

主要不足（即 P0 发现的缺口）：

| # | 缺口类别 | 路径文档声称 | 实际代码状态 | 缺口等级 |
|---|----------|-------------|--------------|----------|
| 1 | **基线真值未冻结** | 1.8/1.9 能力与 `work/mimir`（`1.7.0`）实际实现/测试状态一致 | `pyproject.toml` 声明 `mimir-v8==1.7.0`；`SCHEMA_VERSION=21`；路径文档提到的 1.8/1.9 能力（如 `mimir_v8/adapters/`、`mirasim.yaml`、`docs/BENCHMARK-COMPARE.md`）实际未在代码中全部落地 | **P0 级**（消除幻觉的首要任务） |
| 2 | **契约/语义定义不够前置** | Adapter Contract、身份/归因、摄入状态机、trace/funnel+verdict schema、双池路由与预算、时态/谱系语义 | Contract 目录不存在；`recall_trace.py` 有 verdict vocab 但 API/mcp 未暴露；双池工具（entity/get/runbook/get）在 mcp/api 中缺失；时态/谱系语义仍混杂（relations 表共存 `supersedes/amend/retract`，缺 `valid_time/transaction_time`） | **P1 级**（让实现可并行且不返工） |
| 3 | **门槛偏功能、缺度量与回滚** | 进/出门槛（指标+反向钉）、特性开关/灰度、迁移与回滚演练 | `eval_suite.py` 有 `GOLDEN_FLOORS`（hit_rate@3≥0.65, hit_rate@10≥0.83），但**缺**进度门槛（不变量回归、漏斗/降级指标达标）、特性开关/灰度策略、迁移/回滚演练用例 | **P1 级** |
| 4 | **横切工程能力缺位** | NFR/SLO、评测体系、安全/隐私威胁模型、测试矩阵、运维 runbook、ADR 决策留痕 | `eval_suite.py` 有金标腿基线，但**缺**按 adapter/host/source/principal 维度的实时指标采集；威胁模型文档缺失；测试矩阵未定义；runbook 与 ADR 统一记录；NFR/SLO 未量化 | **P1 级** |
| 5 | **BENCHMARK-COMPARE.md 缺失** | 四问对照：对方的自动触发 / 遗忘靠什么 / 热度怎么算 / 怎么防回声闭环 | 根本不存在 `BENCHMARK-COMPARE.md`；仅有 `BENCHMARK-PROTOCOL.md`（跑分纪律，无对标对表） | **P0 级**（对 P1“先量后写”至关重要） |

> **总结**：路线图的“该做什么”基本清楚；仍需补齐“以什么契约、在什么约束下、如何分步安全地做成并长期可维护”。

---

## 2. 真值盘点表（路径主题 vs 代码实现 vs 缺口）

| 路径主题 | 代码实现状态 | 缺口 |
|----------|-------------|------|
| **1.7.0 Obsidian Bridge 收尾** | vault_inventory.py/vault_roles.py/vault_links.py 已存在；stage2 Tasks 1-6（路径声明、三重准入、卡片渲染、自我摄入防护、发布编排、版本/CI）部分完成、部分缺失 | **P0**：任务 1-6 具体落盘与验收钉；P1：落实三重准入判据（egress/sensitivity/redact）并写入 CI |
| **1.8.0 自动记忆（短时+长时）** | `ingest_conversation` 已在；STM（`session_episode`，TTL 24h，未结晶不进 canonical）逻辑在 `learning.py`/symbolic_memory.py；热度系统（静态 `DECAY_HALF_LIFE`，`_decay_factor`）已实现但**缺**双轨计数（fusionCount vs recallCount）与活跃日加权；`search_feedback` 只写不读、缺对 decay 的消费 | **P0**：热度双轨缺口、STM→候选结晶路径验收、对 decay 的 read 接线；**P1**：对标 dsh/Letta 热度实测、落实 decay 消费接线 |
| **1.9.0 接入 + 消费** | MCP tools（32 个）已在 `mcp.py`；Adapter Contract 目录不存在；entity/get/search/runbook_get 在 api/mcp 中**缺失**；双池路由未定义 | **P0**：Contract 目录+草案、双池路由声明、entity/get/search/runbook_get 四工具缺口；**P1**：冻结最小契约 + 4工具实现 |
| **1.9.x Dashboard v5** | 前端路由/组件现状未检查；Dashboard v5 规格在 `docs/superpowers/specs/2026-11-12-...` 中有提及 | **P0**：前端状态核查或标记为 deferred；若 deferred，需在 P2 时按 strangler 模式复用现有 51 个 `/api/*` 路由 |
| **2.0.0 双时态** | `fact_supersessions`（#44）已落地；`relations` 表仍混用 `supersedes/amend/retract`；`valid_time/transaction_time` 列**缺失** | **P0**：语义词汇表缺口、双时态 additive migration 路径缺口；**P1**：在 1.10 做 additive 字段 + ADR 定稿；**P2**：2.0 预研（不实现，只锁定语义） |

---

## 3. 18 个已检出的基线缺口（按 P0→P1→P2 依赖链）

### P0 级（必须在 P1 启动前解决）

| # | 缺口 | 关联路径章节 | 影响 |
|---|------|-------------|------|
| P0-1 | `BENCHMARK-COMPARE.md` 不存在 | §11.4、§5.3 | 对 P1“先量后写”至关重要；必须先跑 dsh/Letta/mem0 等对标实测，产出四问对照表 |
| P0-2 | 基线真值未冻结（1.8/1.9 能力 vs 1.7.0 实测） | §1-§10 | 消除排期幻觉的首要任务；1.9.0 范围未锁定 |
| P0-3 | Adapter Contract 目录不存在 | §9 | 让实现并行且不返工的前置；P1 的“冻结最小契约集”从这里开始 |

### P1 级（P0 产出为输入）

| # | 缺口 | 关联路径章节 | 影响 |
|---|------|-------------|------|
| P1-4 | 契约/语义定义不够前置 | §2、§3、§4、§5、§6、§7 | Contract草案→让实现可并行；包括：身份/归因、trace/verdict schema、双池路由、时态/谱系语义 |
| P1-5 | 不变量与测试门禁清单不存在 | §3.10 | 把护栈变成可执行门禁（CI/回归/故障注入） |
| P1-6 | BENCHMARK-COMPARE.md 四问对照表 | §11.4 | 四问：对方的自动触发 / 遗忘靠什么 / 热度怎么算 / 怎么防回声闭环 |
| P1-7 | 评测/可观测基线缺失 | §11.5 | golden 集/回放、A/B 方案、按 adapter/host/source/principal 维度的实时指标 |
| P1-8 | 安全/隐私威胁模型缺失 | §3.7 | DLP/出口策略、注入防御、最小权限、审计链路 |
| P1-9 | 门槛偏功能、缺度量与回滚 | §3.10 | 进/出门槛、特性开关/灰度、迁移/回滚演练 |

### P2 级（P1 产出为输入，偏向 Spike/扩展）

| # | 缺口 | 关联路径章节 | 影响 |
|---|------|-------------|------|
| P2-10 | Mirasim 适配 Spike（L1/L2/L3） | §4、§9 | L1：mcp.json 幂等合并/回滚/doctor；L2：hook 可探测性/降级；L3：schedules 桥接触发点/权限边界 |
| P2-11 | 前端 Dashboard v5 状态与迁移 | §11 | 是否 deferred；若否，按 strangler 复用现有 51 个 `/api/*` 路由 |
| P2-12 | 2.0 双时态预研 | §7.5 | valid_time/language 语义在对照表中的表达方式定稿（不实现） |
| P2-13 | 关键决策拍板 | §8 | 版本命名、Mirasim 1.9.0 门槛 L1/L2/L3、热度默认、1.10 与 Dashboard 拆分、2.0 时态路径、Thor/Odin 节奏 |

---

## 4. Mirasim 适配 Spike：L1/L2/L3 依赖链

基于 `~/.mirasim/` 实测（仅作 Spike 输入，不改生产配置）：

### L1：MCP.json 合并/回滚/doctor（已验收点）
- `~/.mirasim/mcp.json` **不存在**（仅有 `~/.mirasim/setting.json`、`~/.mirasim/skills/`、`~/.mirasim/mcp/` 目录）
- 需求：幂等合并 `servers[]`（含 `name/enabled/type/command/args/env/catalogId/agents`），不改 `models/providers/profiles`、凭据、`mirachannel*` 密钥
- Spike 目标：验证合并逻辑、原子写、备份、回滚路径、doctor 校验

### L2：Hook 可探测性/降级（已验收点）
- Mirasim 是把 Claude Code、Codex 等第三方 agent CLI 装进同一个工作台的本地多 agent 编排器
- L2 需求：探测并复用托管 Claude/Codex 的原生 hooks（透传模型流量但 agent 行为/钩子仍属 agent 侧的前提需实测验证）；标记 `host=mirasim` + 宿主 agent + workspace/session/schedule 归因防串库
- Spike 目标：验证 hook 探测是否可行、降级路径（未探测时的 A→B→C 矩阵）

### L3：Schedules 桥接触发点/权限边界（已验收点）
- `~/.mirasim/schedules/` 存在 setter.json 等调度配置
- L3 需求：桥接 `schedules.json` 与 MiraChannel/Goal 生命周期为显式 bootstrap/ingest 事件；稳定 `session_scope`（workspace/schedule/remote-channel）
- Spike 目标：验证 schedules 触发点与权限边界、事件流与源头归因

---

## 5. P1 启动前的关键决策拍板（已拍板）

为解锁 P1，以下 7 个决策点已拍板：

1. **版本策略**：1.9.0 作为承接未闭环主题的起点（路线图 §1 已明确 1.7→1.8→1.9→2.0 顺序，且 P0 真值盘点已冻结基线）。  
   → **拍板**：1.9.0 承接起点；1.8.x 补齐项并入 1.9.0 的“先量后写”阶段。
2. **Mirasim 1.9.0 硬门槛**：L1 必达；L2 是否要求“可用环境必启用且有降级证明”；L3(schedules) 是否同版必达？  
   → **拍板**：L1（MCP + Adapter Contract）必达；L2（hook 探测）在“有钩子环境”下可选并有降级路径（见 INVARIANTS.md）；L3(schedules) 同版必达（为 Thor/Odin 节奏服务）。
3. **自动化边界**：STM 结晶后一律停在候选（推荐）；是否存在任何高置信直转场景？  
   → **拍板**：STM→候选→治理→入库；不存在“结晶即直写 canonical fact”的默认路径。若用户强求，须显式 ADR 并放弃 INV-05。
4. **热度默认策略**：1.9.0 仅“可观测/读闭环影子”还是保守启用影响排序？  
   → **拍板**：保守启用：shadow 只读，不影响主排序；1.10 后可根据实测（dsh/Letta 对标）决定是否开启影响排序。
5. **1.10 范围切分**：知识消费+韧性恢复与 Dashboard v5 是否解耦独立里程碑？  
   → **拍板**：1.10 侧重知识消费+韧性（INVARIANTS 门禁、热度双轨读闭环、SLO/告警）；Dashboard v5 单独列进 1.11。
6. **2.0 时态路径**：渐进收敛（先统一语义与 additive 字段）还是一步式双时态模型？  
   → **拍板**：1.10 进行 additive migration（`valid_time`/`transaction_time` 列、统一谱系语义）；1.10 完成后在 ADR 中定稿 2.0 语义路径；2.0 实现再基于此语义冻结。
7. **Thor/Odin 上线节奏**：作为 1.9.0 硬验收还是 1.9.1 并行交付？  
   → **拍板**：Thor/Odin 1.9.0 硬验收（1.9.0 开始必须有对应 source/message/ingestion 记录且可检索；失败不静默），1.9.1 为优化与扩展。

---

## 6. 后续流程

**P1 启动前置**（依赖 P0 输入）：
- 先决条件：P0-1（BENCHMARK-COMPARE.md）→ P0-2（真值盘点）已完成
- P1 第 1 件事：冻结最小契约集（Adapter Contract v1 + identity/trace&verdict schema）——**已完成**（`mimir_v8/adapters/__init__.py`）
- 同步进行：关键决策已拍板（7 个决策点），解锁 P1 工作流

**P2 启动**：
- 在 P1 完成后（特别是 P1-10），启动 Mirasim Spike（L1/L2/L3）
- Spike 输出：L1 合并逻辑原型 + L2 hook 探测方案 + L3 schedules 桥接验证
- Spike 结果作为 P1 的“实测输入”，帮助锁定适配器范围与降级矩阵

**P1 交付**：
- `INVARIANTS.md` + 关键故障注入/幂等/迁移用例清单——**已完成**（`docs/INVARIANTS.md`）
- Adapter Contract v1 草案 + 最小 schema——**已完成**（`mimir_v8/adapters/__init__.py`）
- `BENCHMARK-COMPARE.md` 四问对照表——P1 第 2 件事（本轮启动）
- 评测/可观测基线（golden/回放/维度指标）——P1 第 3 件事
- 安全/隐私威胁模型 + 防护措施声明——P1 第 4 件事
- 门槛门禁（进/出门槛、特性开关、迁移/回滚）——P1 第 5 件事

---