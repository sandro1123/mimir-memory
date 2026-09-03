# Changelog · 变更日志

> All notable changes to Mímir are documented here.
> Mímir 的所有重要变更记录于此。
> English · 中文双语

---

## v14.0.0 — 2026-09-03 · WikiSkill 技能流水线 + 加密联邦 + 跨模型投影 (AutoSkill · Encrypted Federation · Cross-Model Projection)

> 三件功能 + 升版收尾，全零迁移（无 DDL 变更），SCHEMA_VERSION 维持 20。

- **WikiSkill 技能自动编译流水线**（`autoskill.py` + `/v14/skills/*`，fcd45da）：Traces (L0) → Mímir Wiki (L1/L2) → Hermes Skills (L3) 三层演化链。`record_success` 按主题沉淀成功 trace（幂等 per trace；拒绝 unknown/非 active trace，Fail-Closed）；胜任门槛 = 成功 ≥3 且成员零 negative feedback；`compile_wiki_candidates` 出列候选；`promote_to_skill` 一键审批物化 L3 skill fact（promotion 时再验门槛——ledger 可能已变；幂等：重复晋升同一 fact）。skill 入 `ANCHOR_FACT_TYPES`/`LAYER3_FACT_TYPES`——检索面自动全量挂载，与铁律同一存在保证。REST 面 `write`/`read`/`manage` scope 门（ingest-only 403）。
- **跨节点去中心化加密联邦**（`federation/`，acea7a9）：多台家庭服务器（N100/台式机/云端节点）基于 append-only CRDT 事件流加密同步。federation_events 每次变更一行携带 lamport 时钟+node_id；冲突按 LWW 合并（lamport 高者胜，同刻比 node_id DESC——全序无分叉）；离线容灾：断线期间各自写入，重连按 since 游标交换事件流增量重放，合并可交换（A∘B == B∘A，最终一致）。加密信封 Fernet：出节点加密、入节点按注册 peer 密钥解密——未注册 sender 的密文无法解密（Fail-Closed），篡改信封拒收。`(crdt_key, lamport, node_id)` UNIQUE → 重投递 no-op。federation_peers 注册表带密钥指纹（sha256 前 9 字节 base64，人工核对握手凭据）。
- **跨模型认知语义投影**（`projection.py`，cd5055d）：适配不同模型窗口与输出格式，实现小模型挂载优质技能后的越级能力爆发。MODEL_TIERS 三档：claude（大窗 8k，全保真 markdown）> deepseek（中窗 3k，结构化列表）> local-small（小窗 1.2k，紧凑 KEY: value 方言）。`project_context` 把同一检索面投影成目标模型注入块：L3（iron_rule/user_pref/skill）content 全保真——锚通道保证穿越投影存活，技能永不裁剪（越级能力的全部来源）；L2 按档降级（全文→摘要→硬截断）；L1 所有档只留类型+溯源行（fact_id 可溯源不占预算）。预算守卫从尾部先丢 L1 再丢 L2，永不丢 L3。token 估算保守 2 字符≈1 token，截断必带省略号（无静默截断）。
- **版本号 13.0.0 → 14.0.0**（三锚定：`schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言；SCHEMA_VERSION 维持 20）。

## v13.0.0 — 2026-09-03 · 共享工作黑板 + 时态知识图谱 + 主动前置唤醒 (Blackboard · Temporal Knowledge Graph · Proactive Wake)

> 三件功能 + 升版收尾。SCHEMA_VERSION 19 → 20（relations 增 `valid_from`/`valid_until` 双列，守卫式 ALTER，旧库平滑升级）。

- **多 Agent 共享工作记忆黑板**（`blackboard.py` + `/v13/blackboard/*`）：多 Agent 在排障/分析/研讨时秒级共享局部任务上下文；board 参与者边界（非参与者读写被拒+入参防伪造）、distill 提炼总结沉淀为长期事实、destroy 安销（留 audit 痕）；creator-not-participant 回滚守卫。REST 面 `write`/`read` scope 门。
- **时态知识图谱 TKG**（`schema.py`+`store.py`+`migration.py`+`graph_projector.py`，a0aa913）：relations 增 `valid_from`/`valid_until` 时效窗（空串=开放区间）；supersede 写双向边（supersedes 开窗 + superseded_by 零宽关窗）；`/v13/graph/history?at=ISO8601` 时点快照查询。通用迁移链 `<=18` 恒 rebuild，专用 `migrate_schema_v19` 冻结产出真 19 形。守卫式 ALTER 对新库降 stamp 夹具免疫。
- **主动意图预测性前置唤醒**（`relevance.py` + `/v13/wake`，65e35d3）：IntentProfiler 关键词驱动意图分类（destructive/change/troubleshooting/generic，轻实现不依赖 LLM）；ProactiveWake 前置推送铁律+核心偏好（任何意图永远推送，安全底线）+同意图家族 pattern（排障意图推排障 pattern；generic 不推，宁缺毋滥）；全程过 `can_read` ACL 仲裁（Fail-Closed）。
- **版本号 12.2.0 → 13.0.0**（三锚定：`schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言；SCHEMA_VERSION 断言随 TKG 19→20 对齐）。

## v12.2.0 — 2026-09-03 · 记忆分层装配 + 检索免疫双通道 + 血缘继承 (Layered Assembly · Anchor Channel · Lineage Inheritance)

> 五件全零迁移（无 DDL 变更），SCHEMA_VERSION 维持 19。

### Added · 新增
- **L0~L3 分层装配与渐进式展开（spec 阶段二任务1）** — 检索默认只装配 L3（iron_rule/user_pref）+L2（pattern），L1 原子事实（event/project_config/ephemeral/learning/reference）standard 档不装配、`depth="deep"` 才下钻，大幅削减 Token 消耗；L0（conversation_messages 原始对话）作为证据层检索永不装配。映射零迁移：全部复用 facts.fact_type 既有枚举。分层注入走统一 layer sweep（standard 扫 L2、deep 扫 L2+L1，`LAYER2_BUDGET` 预算内保保存），显式 `fact_type` 过滤器覆盖深度默认（表达精确追溯意图时放行）；hydration 段 L1 门把相似度通道漏进来的 standard 档 L1 拦下并计数 `filtered["layer"]`（可观测）。`trace()` 严格镜像 search()（同 sweep 块+同 L1 门），新增 `LayerSweep` 漏斗阶段，两口径不分叉。
- **检索锚通道（Anchor Channel，spec 阶段二任务4）** — 铁律与用户核心偏好免被语义相似度一票否决：锚通道在候选池构建阶段直接从 canonical 注入活跃 iron_rule/user_pref，不依赖 vector/fts/graph 三通道命中。ACL 仲裁与状态过滤照常在 hydration 执行（锚通道改变「谁能进池」，不改变「谁能被读到」）；`use_anchor=False` 可关；注入量受预算约束（防铁律库膨胀挤占 top-K）；trace() 报告 `AnchorChannel` 阶段（hits/injected/enabled）。
- **统一 Profile 视图 API（/v12/profile，spec 阶段二任务2）** — 跨 L3~L1 一站式只读聚合：iron_rule/user_pref/pattern/event/project_config/learning/reference 按 owner+domain 过滤直查 canonical，带 ACL，为上层「人格视图」消费提供单一入口。
- **XTMEM 血缘最严继承 + Fail-Closed（spec 阶段二任务3）** — supersedes 链上 visibility/sensitivity/egress_policy 三档各取 max(来源，提案)——继承自来源且提案永不放宽；幽灵来源（supersedes_fact_id 不存在）Fail-Closed 抛 `CandidatePolicyError`（422 面），schema FK 是最后防线（500 面）；继承落在 proposed_* 列，commit_approved 直读即贯穿。`create_candidate_in_transaction` 单一卡点全链覆盖。

### Fixed · 修复
- **disputed 投影同步闭环（spec 阶段二任务5）** — 两处真凶双杀：①`_mark_disputed` 裸 INSERT `memory_events` 无 outbox 行——`fact.conflict_lost` 永不进投影流，fts/graph/vector/core_memory 四投影继续按 active 服务败者（读到已被否决的事实）；修法=对齐 store 既有 `_insert_version_and_side_effects` 先例，向 `PROJECTORS` 全扇出 pending。②conflict.py 三事件 payload_hash 是自造串（非 `sha256(payload_json)`）——`verify_canonical` 一致性门禁对冲突事件必报 `event_hash_mismatch` 误报；修法=三处改对 payload 本身求哈希，verify 误报清零。

### Changed · 变更
- **API 层透传 v12.2.0 检索参数（收尾件）** — QueryBody 增 `depth`/`use_anchor` 两字段，`/v8/query` 与 `/v12/search/trace` 两构造点透传进 QueryRequest；此前 REST 调用方被永久锁死在 standard 档+锚常开——内核能力已存在但对 API 使用者不可达。`/v8/query` 对非法 depth 答 422（对齐 dedup_threshold 先例）。
- **版本号 12.1.4 → 12.2.0**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。SCHEMA_VERSION 维持 19——五件全零迁移，无 DDL 变更。

---

## v12.1.4 — 2026-09-02 · 采集管道三缺口补全 + Schema v19 (Collector Wiring + Schema v19)

### Fixed · 修复
- **vault 采集物分类补键（通电前审计发现）** — `classifier.SOURCE_CATEGORY_MAP` 无 `"vault"` 键，vault 笔记经 collect_all 摄入后落 `unknown/quarantine`——隔离数据无下游消费者可用。修复：vault 归类 `knowledge_doc`（与 feishu/file/document 同族本地知识文档；`KNOWLEDGE_DOC_TYPES` 同步），extraction 闸门（仅放行 `conversation` 类）自然将其挡在 LLM 提取之外——vault 全文只落库不外呼。
- **collect_all 透传 per-source `exclude_dirs`** — `worker.py` vault 分支未把 config 的 `exclude_dirs` 传给 `VaultCollector`（只传 vault_root），生产 vault 含明文凭据目录（敏感扫描 7 文件命中）无 config 层排除手段。修复：per-source `exclude_dirs` 与内置默认四目录（.obsidian/.git/.trash/.smart-env，`DEFAULT_EXCLUDE_DIRS`）取并集——配置的排除名单不会静默丢掉默认项。
- **web 源幂等键加内容指纹** — `web:<sha256(url)>` 只锚 URL：页面内容更新后第二次采集必撞 `ConflictError`（"idempotency key was reused with different content"）进 `results["errors"]`，web 源通电后首内容变更即断流。修复：key 改 `web:<sha256(url)>:<sha256(content)>`，内容变更采集为新版本，同内容仍幂等去重。

### Added · 新增
- **Schema v19：conversation_sources connector_type 解除旧 CHECK 冻结（vault 首采实弹发现）** — 生产库 v8 建库时 connector_type 冻结在七个旧类型的 CHECK 白名单，vault 首采（2026-09-02）全量 `IntegrityError`（415 篇零落库）；dev 新库 DDL 无此 CHECK（宽松），测试全绿掩盖了生产拒绝——新旧库 schema 漂移。修复方向判例（首版收紧、门禁 7 真红复盘后定稿）：**classify() 拥有未注册类型的 quarantine 路由权，DDL 不设卡**——生产 v8 库重建后与 dev 新库一致为无 CHECK 宽松态（7 个存量 unknown_xyz 测试锚定「可插入但被隔离」契约）。修复件：`schema.py` SCHEMA_VERSION 18→19；`migration.py` 新增 `migrate_schema_v19()`（表重建：SQLite 不能 ALTER CHECK，新表→拷行→改名，行数 before/after 校验 + foreign_key_check，沿 12-step ALTER 惯例），主链 `migrate_schema` 白名单放宽至 {9..18}→{11..19} 并在 additive 链后接 v19 重建。

9 项新测试（tests/test_p22_collector_wiring.py，含 legacy CHECK 造库→迁移→vault 可插入端到端 + 新库同约束）。RED→GREEN：三缺口 5 红 + v19 2 红 → 绿 9/9。

### Changed · 变更
- **版本号 12.1.3 → 12.1.4**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.3 已推公共远端不可重写，且锚定的是三缺口修复（28cd706）、不含其后的 v19 表重建三笔——Schema v19 以独立 tag v12.1.4 锚定部署树。

---

## v12.1.2 — 2026-09-01 · 写路径注册表半通电补全 (Write-Path Registry Completion)

### Fixed · 修复
- **静态集消费者全迁动态注册表（部署实弹验收发现）** — v12.1.1 通电了 `validate_agent_id()` 半边，但 6 处写路径消费者仍查静态 `AGENT_IDS`/`DOMAINS` frozenset：quantstar 经 config federation 注册后仍被写路径拒收（生产活进程铁证：`POST /v8/facts` 422 `invalid owner_principal: quantstar`；DB 中 quantstar 存量 15 条系旧热修时代写入，新写入被堵）。修复：全部迁移到 `get_registered_agents()`/`get_registered_domains()`——
  - `schema.py` `CreateFact.validated()`（owner+domain 两查，POST /v8/facts 主写路径）
  - `learning.py` `remember()`（显式记忆摄入，agent+domain）
  - `core_memory.py` `promote()`（canonical 晋升闸门）
  - `api.py` `crystal_approve`（回落 mentor 前先查动态集——注册 agent 不再被静默改派）
  - `knowledge.py` `create_item`（domain 闸门，grep 复查新发现）
  - `evaluator.py` domain 白名单（`ALLOWED_DOMAINS = frozenset(DOMAINS)` 导入期冻结——改为 `_allowed_domains()` 每次评估现算，grep 复查新发现）
  负向守护：未注册 agent/domain 依旧拒收不变。11 项新测试（tests/test_p21_federation_write_paths.py），含「消费者模块不得 import 静态集」源级总闸。

### Changed · 变更
- **版本号 12.1.1 → 12.1.2**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.1 已推公共远端不重写，热修以独立 tag v12.1.2 锚定部署树。

---

## v12.1.1 — 2026-09-01 · 动态注册表通电 (Federation Registry Wiring)

### Fixed · 修复
- **动态注册表通电（部署前审计发现）** — `register_agent()/register_domain()`（v12.1.0 任务1，d91b0fc）建成后全库零调用点（教科书式「建了没通电」），部署 v12.1.0 将使 quantstar（生产硬编码热修注册）写入直接校验失败。修复：`config.py` 新增 `load_federation_registry()`——读 `mimir_config.yaml` 可选 `federation.agents/domains` 段逐项注册；缺文件/缺段静默跳过（worker 无 config 可跑），结构错 ValueError 决不静默（铁律#12）。挂点双入口：`worker.main` 与 `runtime.build_runtime`，server/worker 进程全覆盖。8 项新测试（tests/test_p20_federation_bootstrap.py），注册表模块级集合有快照/还原隔离。

### Changed · 变更
- **版本号 12.1.0 → 12.1.1**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.0 不重打（公共远端已发布），热修以独立 tag v12.1.1 锚定部署树。

---

## v12.1.0 — 2026-08-31 · Eval 安全网 (Eval Safety Net)

### Added · 新增
- **Mímir-Eval 评测套件** — `mimir_v8/eval_suite.py`。纯函数指标层 + 种子化合成基准。
  - 检索指标：`hit_rate@K`（查询级：任一 ground-truth 进 top-K 即 1.0）与 `mrr`（仅首个命中：1/rank，未命中 0.0）；空 ground_truth 拒绝计分（ValueError）。
  - 抽取指标：precision / recall / F1，集合语义（重复只计一次）。
  - ACL 泄漏率：检索行中未授权占比——生产地板值为 0.0，任何非零值即安全回归。
  - **诚实遥测**：合成基准在报告与摘要两级均盖 `provenance="synthetic"` 章，合成数字永远无法伪装成生产质量；真实地板值在在线金标集（tests/test_r9_eval.py），不在此处。
  - 双语（CJK+latin）12 主题合成语料 + 固定种子可复现；19 项新测试（tests/test_p18_mimir_eval.py）。
- **全源采集统一调度管道** — `worker.collect_all` 升级为配置驱动的源注册表（`collector.sources`：rss / web / vault），单一调度入口跑全部启用源。
  - **Vault (Obsidian) 采集器** — `collectors/vault.py`：扫描 markdown 笔记库转 CollectResult；排除隐藏目录（.obsidian/.git/.trash/.smart-env）与 template.md；幂等键 `vault:<relpath>:<mtime>`（改过的笔记以新版本再采，未动过的去重跳过）。
  - **配置驱动源注册表** — `load_source_registry`；无配置回落旧版 RSS-only 行为（存量部署零影响）；未知源类型抛 ValueError，绝不静默跳过；单源失败隔离进 `results["errors"]` 不中断其他源。
  - **幂等键统一** — RSS `rss:sha256(url|title)`、web `web:sha256(url)`、vault `vault:relpath:mtime`，逐条入库防重。
  - Web 采集错误从 `collect_url` 的静默吞没中上浮到 `results["errors"]`。
  - 8 项新测试（tests/test_p19_ingestion_pipeline.py）。

### Changed · 变更
- **版本号 12.0.2 → 12.1.0**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml`）。
- （master 先行合入）动态 agent 注册表 d91b0fc——v12.1.0 任务1。

---

## v12.0.2 — 2026-08-18 · 安全与隔离修复 (Security Hardening)

### Security · 安全修复
- **ACL 联邦隔离 (High)**：修复 `store.can_access` 中对所有已认证主体无条件注入 `federated_agents` 角色导致 `shared` 可见性塌陷为 `all` 的缺陷。
- **特权分离 (High)**：移除 `learning.py` 中所有硬编码的 `actor_principal != "sandro"` 特权绕过分支，统一由系统鉴权层控制。
- **越权修复 (Med->High)**：修复 `GET /v10/opinions` 与 `GET /v10/observations` 列表端点未按调用主体做 IDOR 隔离的问题。
- **符号记忆租户隔离 (Med)**：在 `symbolic_blocks` 和 `symbolic_canvases` 表中加入 `owner_principal` 字段并建立索引，API 层强制租户鉴权。
- **Web 采集器 SSRF 防御 (Low)**：为 `WebCollector` 增加协议限制与私有/回环/保留 IP 阻断检查。
- **实体解耦 (Low)**：将 `relevance.py` 中硬编码的私有拓扑与 Agent 名单解耦为通用领域术语。

---

## v12.0.1 — 2026-08-16 · 读侧打通 (Hermes MemoryProvider live)

### Added · 新增
- **Hermes MemoryProvider flat-layout plugin** — `hermes-plugin/mimir_memory_provider/`。
  实现 `agent.memory_provider.MemoryProvider` ABC：`prefetch()` 每轮召回注入，
  `get_tool_schemas()`/`handle_tool_call()` 暴露 4 个工具。写路径保持
  mimir-v9.2-cdc 不变（避免重复抽取）。
- **`/v8/query` 审计** — `execute_query` 每次成功检索写 `audit_log(action='query')`，
  使 `/v12/evolve/report` 的 `audit_query_count` 有真实数据。
- **install.sh 重写** — 安装目录布局 + 配置提示。

### Fixed · 修复
- **插件 search 请求体** — 全部三处 `{"query": ...}` → `{"text": ...}`，修复
  `/v8/query` 422 静默失败。
- **Hermes healthcheck** — gateway 检查从 system scope 改为 user scope。
- **README** — schema 徽章 14 → 18；Roadmap 表更新。

### Deployed · 部署
- `memory.provider: mimir_memory_provider` + `plugins.enabled` 配置更新。
- 4 个 gateway + mimir.service 已重启，查询流与首条 useful 反馈已验证入库。

---

## v12.0.0 (schema 18) — 2026-08-14 · 代号 Insight (借鉴 aiduMEI v18.3)

### Added · 新增
- **M1a Ebbinghaus 三轨遗忘** — `DECAY_TIER_MAP` 细化，新增 `L5_ephemeral`
  （半衰期 7d），降权不删行。
- **M1b Chronos 双时间轴** — facts 增加 `valid_from/valid_to`；过期降权 50%。
- **M1c EvolveMem** — `search_feedback` + `quality_metrics` 表；systemd
  `mimir-v12-evolve` 每 6h 聚合，有用 +0.05 / 无用 -0.05。
- **M1d Hermes MemoryProvider 插件** — 钩子 on_turn_start/on_turn_end/
  before_context_compress/on_memory_update。
- **M2a 召回漏斗** — `QueryKernel.trace()` 五阶段；`POST /v12/search/trace`。
- **M2b Dashboard** — 新增「检索」tab：漏斗可视化 + 质量看板。
- **M3a 冲突消解** — `conflict_resolutions`（schema 16），败方置 disputed 不删行。
- **M3b 技能结晶** — `crystal_candidates`（schema 17），7 天 topic 聚类 ≥3 → 候选，
  人工 approve 才结晶。
- **M3c MCP 扩展** — 27 个工具。
- **M4 包装** — PyPI、Dockerfile 自包含、Obsidian Wikilink 双链、多模态（schema 18）。

### Changed · 变更
- **Version**: 11.0.0 (schema 14) → 12.0.0 (schema 18)
- Tests: 113 → 198 passed + 23 subtests。

---

## v11.0.0 (schema 14) — 2026-08 · 全量升级 (借鉴 TencentDB Agent Memory)

### Added · 新增
- **Symbolic short-term memory**（`symbolic_memory.py`）— Mermaid canvas 卸载引擎。
- **CodeGraph**（`code_symbols`, `code_relations`）— 代码符号索引、调用图、影响分析。
- **v11 API**：`/v11/symbolic/*`, `/v11/code/*`。
- **`/v10/reflect/{topic}`** — 从相关 facts + opinions 合成洞见。
- **`/v10/federation/{peer_hierarchy:path}`** — 跨主体的 ACL 共享搜索。
- **Dashboard**：2 个新 tab（符号、代码）+ Claude 风格重设计。

### Changed · 变更
- **Version**: 10.1.0 → 11.0.0, Schema 13 → 14
- Dashboard frontend 全量重写（CSS 变量、明暗切换、移动端底导航）。

---

## v10.0.0 (schema 13) — 2026-08-11

### Added · 新增
- **Governance pipeline** 进入包内（`mimir_v8/governance.py`）
- **Opinion confidence evolution** — set_opinion 端点 + evolve ±0.1 调整
- **Opinion consolidation** — ≥3 同主题高置信生成 observations
- **fast_track auto-commit** — 审核通过的 provisional 候选经 HTTP 提交
- **Dashboard 代理端点**：/api/opinions, /api/observations, /api/governance/decisions

### Changed · 变更
- QueryKernel 增加 `include_provisional` 参数
- systemd units 从 v9.3.0 → v10.0.0 升级

### Fixed · 修复
- 缓存 key 死键累积、Dashboard 写操作 Bearer token 鉴权、opinions 分组查询

---

## v9.x — 2026-08

- v9.3.0 (schema 12)：全量 v9.0 发布，搜索预览、事实全状态查询
- v9.0.0 (schema 12)：嵌入 → 规范化存储 + 投影 + 首次审计
- v8.1.0 (schema 11)：候选管线「filter-confidence」路径、Jaccard 去重
- v8.0.0：初始事件溯源 API 结构
