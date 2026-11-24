# Mímir 架构审视与迭代治理建议（1.9.x → 1.10.x → 2.x）

> **日期**：2026-11-19  
> **状态**：Architecture Review / Governance Draft（供评审与排期校准）  
> **关联文档**：`docs/plans/2026-11-19-mimir-1.9-to-2x-development-path.md`  
> **依据（已核对）**：  
> - `pyproject.toml`（`mimir-v8==1.7.0`）  
> - `docs/superpowers/specs/2026-11-12-mimir-1.7-to-2.0-roadmap-design.md`  
> - `docs/superpowers/specs/2026-10-04-mimir-aidumei-adoption-design.md`  
> - `docs/plans/2026-09-08-adumei-benchmark.md`  
> - `mimir-open-source/docs/superpowers/specs/2026-04-11-mimir-1.8-agent-adapter-and-memory-source-extensibility-design.md`  
> - 本机实况：`~/.mirasim/setting.json`、`~/.mirasim/skills/mirasim-guide/SKILL.md`、`~/.hermes/skills/devops/mirasim-agent-ops/SKILL.md`（Mirasim 为托管多 Agent 编排壳；`mcp.json` 为 `{"servers":[...]}` 数组结构并含 `agents` 作用域；存在 schedules/MiraChannel 等编排面）

---

## 1. 总判断：方向对，但“执行蓝图与治理”还不够

**结论**：`2026-11-19-mimir-1.9-to-2x-development-path.md` 的主题划分与护栏（治理不稀释、诚实判语、先量后写、fail-closed、声明式适配）是正确的，符合 Mímir“可信底座的记忆系统 + Agent 知识库”定位；**但它更像版本主题路线图，不足以直接作为可控交付的工程与治理方案**。

主要不足：
- **基线真值未冻结**：路线图所述 1.8/1.9 能力与 `work/mimir`（1.7.0）实际实现/测试状态存在偏差风险（如 `mimir_v8/adapters/`、`mirasim.yaml`、`docs/BENCHMARK-COMPARE.md` 缺失）；未先盘点就排期，易返工。
- **契约与语义先于功能的定义不够细**：Adapter Contract、身份/归因模型、摄入管线状态机、trace/funnel 与 verdict schema、双池路由与预算规则、时态/谱系语义，需先版本化定稿再规模化铺功能。
- **里程碑过载且门槛偏“功能项”**：缺少按依赖与风险切片的交付顺序、量化进/出门槛（metrics + 反向钉）、特性开关与回滚策略。
- **横切工程能力缺位**：NFR/SLO、可观测与评测体系、安全/隐私威胁模型、迁移与兼容策略、测试矩阵、运维 runbook、ADR 决策留痕未系统化。

**一句话**：路线“该做什么”已基本清楚；还需补齐“以什么契约、在什么约束下、如何分步安全地做成并长期可维护”。

---

## 2. Mímir 的架构定位与不可妥协的不变量

### 2.1 定位
- 不是 mem0 式“尽可能自动写入”的记忆产品，也不是纯聊天摘要缓存；而是**可审计、可追溯、可治理**的记忆与知识底座，服务多 Agent/多入口长期协作。
- 竞争力不在“功能数量”，而在：治理管线、事件溯源与谱系、诚实可观测、联邦/权限边界、可证伪评测。

### 2.2 系统不变量（应固化为测试与门禁）
1. **写入治理**：自动捕获/反思/合并/精炼/回写产物进入候选或暂存，不绕过候选→评审→入库；LLM 仅建议不 commit；对 canonical facts 的 UPDATE/DELETE 不自动直写（除非显式翻案并全审计）。
2. **诚实判语**：`recall_verdict`/通道状态对外一致；`degraded ≠ empty`；预算耗尽、能力缺失、降级必须可见且可解释。
3. **Fail-closed**：账本/溯源写失败不执行不可逆删除；无模型不伪造摘要/判重结论；不确定则降级或留候选，不污染真相源。
4. **幂等与可重放**：摄入、hook、补偿重放具备幂等键与去重；重试不产生重复副作用。
5. **归因与隔离**：每条记忆/事件可追溯 `connector_id/owner_principal/session_scope/agent|host`；跨主体不串库，遵循 ACL/egress 策略。
6. **先量后写**：热度、衰减、时态加权、压缩/精炼等策略以靶子量化与 baseline 对照为前置，默认保守可回退。

> 建议：将上述不变量沉淀为 `docs/INVARIANTS.md`（或等价规范）并映射到 CI/回归钉与故障注入用例。

---

## 3. 规范与优化清单（按领域）

### 3.1 架构边界与 Adapter Contract（高优先级）

**现状/风险**：路线图已提出 Adapter Contract，但基线未落地；Mirasim 被列为 B 档/MCP 原生，实际是“托管多 Agent 编排壳”，若按普通 MCP 客户端处理将遗漏 hooks 复用、schedules、MiraChannel、workspace 身份与配置形态差异。

**规范建议**：
- **Contract 版本化**：`mimir_v8/adapters/` 承载 `contract.yaml`（schema 版本）、校验器与各 harness manifest（`claude-code/codex/hermes/mirasim/opencode/deepseek...`）。字段至少含：`name/kind(hook|mcp|explicit|hybrid)/capabilities/capture_triggers/bootstrap/ingest/mcp_inject/identity/constraints/install`。
- **能力协商与降级矩阵**：声明式给出“可探测能力→启用档位→不可用时降级路径”（A→B→C）；禁止静默假设 hooks 可用。
- **安装器语义**：`mimir-mcp install-adapter`（或等价）必须支持 `--dry-run`、幂等合并、备份、原子写、回滚、`doctor` 校验；明确各宿主配置格式差异。
- **Mirasim 专项规范**（基于本机实况）：
  - L1：合并 `~/.mirasim/mcp.json` 的 `servers[]`（非对象式），正确处理 `name/enabled/type/command/args/env/catalogId/agents`；不改 `models/providers/profiles`、凭据、`mirachannel*` 密钥。
  - L2：对托管 Claude/Codex 探测并复用原生 hooks（透传模型流量但 agent 行为/钩子仍属 agent 侧的前提需实测验证）；标记 `host=mirasim`。
  - L3：桥接 `schedules.json` 与 MiraChannel/Goal 生命周期为显式 bootstrap/ingest 事件，稳定 `session_scope`（workspace/schedule/remote-channel）。
  - 身份：`connector_prefix=mirasim` + 宿主 agent + workspace/session/schedule 标识组合，防串库与便于审计。

**优化点**：以 Mirasim 作为**参考实现/狗食适配器**（复杂度最高、价值高），先跑通 contract→安装→降级→观测全链，再横向扩展低复杂度适配器。

### 3.2 摄入与记忆生命周期（正确性核心）

**规范建议**：
- **统一管线**：`Source Event -> Envelope(归因/幂等键/原文指纹) -> 校验+DLP/脱敏 -> 去重与幂等 -> STM/episode(TTL) -> 结晶(规则优先, 无模型不伪造) -> Candidate -> Review -> Canonical(Fact/Entity/Runbook)`；每步可观测、可失败、可回放。
- **STM 与 Canonical 边界**：STM 仅会话/短窗可见，不作为真相源；结晶产物默认候选；保留与清理受 TTL 与账本约束。
- **热度/反馈语义**：明确并分离 `exposure/search`、`fusion`（内容重复/合并）、`recall/attend`（确认使用）计数与消费点；读反馈影响排序/衰减需保守参数、可开关、可 A/B 与回退（对齐“先量后写”）。
- **删除与保留**：衰减/遗忘遵循账本先行（tombstone/等价记录写成功才删），支持可恢复路径设计空间（1.10.x 方向）但不破坏现有治理。

**优化点**：补齐“回声闭环”防护（重复检索不等于价值提升）与来源置信度在排序中的诚实表达。

### 3.3 知识库与双池（1.10 前置的接口规范）

**规范建议**：
- **双池边界**：Memory（事实/偏好/事件/模式等治理记忆）与 Knowledge（实体/Runbook/引用知识）查询路由采用**确定性规则**，结果标注 `pool`，预算独立统计与上报，不混排串账。
- **消费契约**：MCP/REST 的实体与 Runbook 查询、实体 supersession 消费、执行回写（如 `battle_tested`）需先定 schema 与权限/审计语义，再实现；知识变更仍受治理。
- **可追溯性**：召回结果携带来源、时态/有效性与谱系线索，支撑“为何召回/为何未召回”。

### 3.4 时态与谱系语义（为 2.x 降低迁移成本）

**风险**：`occurred_at/recorded_at/valid_from|to` 与未来 `valid_time/transaction_time`、`fact_supersessions` 与 relations/实体取代表达并存，易造成查询歧义与迁移返工。

**规范建议**：
- **语义词汇表**：定义各时间字段与“取代/失效/生效”含义及优先级；在 1.9/1.10 仅做 additive 与文档/契约澄清，不提前引入冲突语义。
- **单一谱系真相源策略**：尽早选定并收敛（推荐结构化 supersession 为真相源、其他为视图/兼容层），以 ADR 记录。
- **迁移纪律**：守卫式迁移、可空/可降级回填、双读兼容窗口、差异量化与回滚预案；禁止大版本一次性语义硬切。

### 3.5 召回可观测与诚实面

**规范建议**：
- **Trace/Funnel Schema 版本化**：每阶段 `{name, verdict, hits, elapsed_ms, detail}`，覆盖候选池→多路召回→去重→衰减/过滤→融合→裁剪/最终；通道故障显式 `degraded` 并传导至总体 verdict（按既有语义）。
- **对外契约先行**：API/MCP 字段与预算/降级标记在 1.9.0 冻结最小集；Dashboard/可视化消费契约而非反向驱动后端破坏性变更（遵循 51 路由与 strangler 原则）。
- **解释性**：为“未命中/降级/被过滤/被取代”提供机器可读原因码，支撑调试与信任。

### 3.6 可靠性、性能与成本（NFR）

**规范建议**：
- **SLO/预算（建议量化并纳入门禁）**：hook/摄入额外时延上限、召回 p95、摄入端到端 lag、后台任务资源占用、单会话/单租户记忆增长与 LLM/embedding 调用预算。
- **背压与削峰**：自动捕获引入突发流量，需队列/批处理/采样与速率限制；避免 SessionEnd 风暴拖慢宿主。
- **韧性**：LLM/embedding/存储依赖的超时、熔断、降级路径；断供欠账与补偿重放需幂等、限流、可审计（可借鉴 aiduMEI，但纳入治理与可观测）。
- **Lean 能力分层**：FTS-only/轻量挡位作为显式 profile 与能力声明，行为与质量预期可文档化。

### 3.7 安全与隐私

**规范建议**：
- **威胁模型（需文档化）**：被捕获内容含注入指令/敏感信息/越权请求；适配器安装与配置篡改；跨主体数据泄漏；恶意或异常 payload 导致资源耗尽。
- **防护**：入口 DLP/脱敏与出口 egress 策略一致执行；记忆内容作为“数据”而非“指令”注入（对齐 aiduMEI/Mímir 既有纪律）；最小权限与主体隔离；安装器仅写声明范围配置且不读取/输出凭据。
- **审计**：配置变更、摄入来源、候选流转、入库/取代/删除/恢复均可追溯。

### 3.8 可观测性与评测体系（当前最大工程缺口之一）

**规范建议**：
- **对标基线**：补齐 `docs/BENCHMARK-COMPARE.md`（触发/遗忘/热度/防回声四问，标注证据与版本/日期）；对 aiduMEI、mem0、Zep/Letta、claude-mem、dsh 生态、Cognee 等分优先级实测或复核。
- **内部评测**：golden 集与回放、召回 verdict 分布、A/B/C 档捕获漏斗（覆盖/幂等命中/误捕获/候选转正）、热度策略 A/B（默认 off 或保守）、知识消费正确性用例。
- **运行指标**：按 adapter/host/source/principal 分维度的摄入量、lag、失败率、降级率、重放积压、存储增长；告警与报表（含 Thor/Odin、Mirasim schedules/remote）。
- **门禁**：关键不变量与核心指标回归纳入 CI/夜间任务；“先量后写”产出作为特性开启前置 artifact。

### 3.9 测试与质量保证

**建议测试矩阵**：
- **单元/性质测试**：不变量、状态机、幂等键、schema/迁移、计数与衰减函数。
- **集成测试**：每个 adapter 档（含 Mirasim `servers[]` 合并、hook 探测与降级、schedules 桥接）的端到端；MCP/REST 契约测试。
- **故障注入**：LLM/embedding 不可用、账本写失败、部分写入/重试、超时与背压、畸形 payload。
- **迁移与兼容**：前向/回滚、旧数据回填、双读窗口一致性。
- **性能烟测**：摄入与召回在代表性数据量下的时延与资源基线。

### 3.10 兼容性、运维与开发者体验

**规范建议**：
- **版本与弃用策略**：SemVer 语义明确（尤其 schema/contract/API）；additive 优先，破坏性变更需公告期、迁移工具与兼容层。
- **特性开关**：auto-capture、反馈读闭环/热度、补偿重放、知识路由等高风险能力可灰度/按来源启用。
- **运维 runbook**：Thor/Odin 与 Mirasim 来源接入、回填与重放、tombstone 恢复、adapter 安装/回滚、降级排障。
- **ADR 与文档 SSOT**：关键决策（Mirasim 分层、STM 不入 canonical、热度口径、时态收敛方案、谱系归一）以 ADR 记录；路径/评审/对标/不变量文档交叉引用，降低“模型/人员失忆”。

---

## 4. 对 1.9.x / 1.10.x / 2.x 迭代路径的架构师建议

### 4.1 1.9.x（Connect & Capture）— 先“地基与契约”，再“扩面”

**建议重切片（按依赖与风险）**：
1. **1.9.0-foundation（契约与基线）**：实现真值盘点与 gap 清单；冻结 Adapter Contract v1 + identity/归因 + trace/verdict 最小 schema；补 `BENCHMARK-COMPARE.md` 初版；确定版本命名策略与门槛。
2. **1.9.0-mirasim-L1（参考适配）**：安装器+`mcp.json` 幂等合并/回滚/doctor；MCP 可用性与知识/记忆工具可达；全链路可观测。
3. **1.9.0-capture-core**：A/B/C 分档摄入与幂等、STM→候选结晶、诚实 verdict/trace 输出；Mirasim L2（探测启用+降级）在可验证前提下并入或紧随。
4. **1.9.1-orchestration & rollout**：Mirasim L3（schedules 优先）、Thor/Odin 来源纳入、OpenCode/DeepSeek 扩展、热度 v1.1（保守、可回退）。
5. **1.9.x-hardening**：指标达标驱动的校准与修复，不扩语义范围。

**进/出门槛（示例）**：各档捕获漏斗指标可观测；故障注入下 verdict 与 fail-closed 通过；Mirasim 安装/降级/回滚/不触凭据通过；无 P0 不变量回归；回滚演练完成。

### 4.2 1.10.x（Consume & Recover）— 价值释放建立在 1.9 观测达标之上

- 以**知识消费契约**与**恢复/重放幂等性**为核心，而非 UI 先行；前端改造坚持契约不变与 strangler。
- 欠账重放、tombstone 恢复、证据融合等能力必须“可审计、可限流、可验证”，并纳入 egress/DLP 与治理。
- 仅当 1.9 捕获质量与噪声指标在可控范围内，才扩大自动化与默认策略强度。

### 4.3 2.x（Converge & Deepen）— 语义演进小步快跑、可回退

- **2.0.x 前移设计风险**：在 1.10.x 允许 additive 准备（字段/索引/兼容读）与语义 ADR 定稿，降低一次性迁移面。
- **2.0.x** 聚焦时态与谱系收敛；**2.1.x** 聚焦统一检索与解释；**2.2.x** 聚焦规模/联邦/lean；**2.3.x+** 认知增强受闸门约束。每步独立验收与回滚。
- 避免将“人格/强推理/黑盒自改进”作为目标牵引，防止偏离可信底座定位。

### 4.4 迭代节奏与治理机制

- **度量驱动而非日历驱动**：关键能力默认灰度/保守，达指标后再扩大。
- **Workstream 并行但受依赖约束**：contract/schema → 参考适配器 → 捕获与观测 → 扩展与消费 → 语义演进。
- **变更控制**：高风险行为走 feature flag + dry-run/shadow；每个里程碑附升级/回滚说明与已知限制。
- **决策留痕**：版本策略、Mirasim 门槛、热度口径、时态方案等以 ADR/评审纪要落盘并与路径文档互链。

---

## 5. 建议的近期行动（优先级）

| 优先级 | 行动 | 产出/验收 | 目的 |
|---|---|---|---|
| P0 | **实现真值盘点** | 1.8/1.9 相关代码/测试/schema/gap 清单（对照路线图逐项标注 done/partial/missing） | 消除排期幻觉，校准 1.9.0 范围 |
| P0 | **冻结最小契约集** | Adapter Contract v1 草案 + identity/归因字段 + trace/verdict schema（文档+校验器雏形） | 让实现可并行且不返工 |
| P0 | **Mirasim 适配 Spike** | 基于实测验证 `mcp.json` 合并、hook 可探测性/降级、schedules 触发点与权限边界 | 降低首个复杂适配器不确定性 |
| P1 | **对标与评测基线** | `docs/BENCHMARK-COMPARE.md` 初版 + 捕获/召回指标定义与采集点 | 支撑“先量后写”与参数取舍 |
| P1 | **不变量与测试门禁** | `INVARIANTS.md`（或等价）+ 关键故障注入/幂等/迁移用例清单 | 把护栏变成可执行门禁 |
| P1 | **拍板关键决策** | 版本命名策略、1.9.0 Mirasim 门槛（L1+L2+L3范围）、自动入库边界、1.10 与 Dashboard 拆分、2.0 时态切入方式 | 解锁实施计划与资源分配 |
| P2 | **1.9.0 可执行实施计划** | 分 workstream 的任务/依赖/负责人位/验收钉/回滚方案 | 从路线图进入受控交付 |

---

## 6. 需明确拍板的问题（决策清单）

1. **版本策略**：以 1.9.0 承接未闭环主题，还是先 1.8.x 补齐再开 1.9.x？（影响对外预期与变更记录）
2. **Mirasim 1.9.0 硬门槛**：`L1` 必达；`L2` 是否要求“可用环境必启用且有降级证明”；`L3(schedules)` 是否同版必达？
3. **自动化边界**：STM 结晶后一律停在候选（推荐）；是否存在任何高置信直转场景？若有，审计与回滚形态如何？
4. **热度默认策略**：1.9.0 仅“可观测/读闭环影子”还是保守启用影响排序？启用阈值与回退条件？
5. **1.10 范围切分**：知识消费+韧性恢复与 Dashboard v5 是否解耦独立里程碑？
6. **2.0 时态路径**：渐进收敛（先统一语义与 additive 字段）还是一步式双时态模型？偏好前者以控风险。
7. **Thor/Odin 上线节奏**：作为 1.9.0 硬验收还是 1.9.1 并行交付（取决于来源接入成本与稳定性）？

---

## 7. 结语（架构师视角）

Mímir 的正确演进方式不是“更快地自动记更多”，而是**在更广的接入面与更强的自动化下，仍然保持可治理、可解释、可回放**。因此：
- **短期（1.9.x）**赢在契约与观测：把 Mirasim 等复杂宿主“接稳、分清、看诚实”；
- **中期（1.10.x）**赢在消费与韧性：让知识真正可用、故障可恢复；
- **长期（2.x）**赢在语义统一与受控智能：时态/谱系/统一检索逐步收敛，认知增强只在证据与治理闸门通过后释放。

只要不变量与度量门禁先行，这条路径可以在不牺牲 Mímir 护城河的前提下，缩小与主流记忆产品在“易用与自动化体验”上的差距，并保住在“可信与可运营”上的长期优势。