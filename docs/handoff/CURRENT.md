# CURRENT — Mimir 开发与发布状态（随仓维护）

> 更新时间：2026-11-19（只读盘点 + 需求登记）
> 权威跨项目状态：`~/work/docs/STATUS.md`；SSOT 映射：`~/work/docs/SSOT_AND_RELEASE_MAP.md`

## 当前基线
- 生产 API：`mimir.service` active，`mimir-api 1.7.0`，schema 21，数据目录 `~/.hermes/mimir/v9/<prod-data-dir>`，`facts=1860`，projector 无积压/无死信（就绪态）。
- 生产 Dashboard：`mimir-dashboard.service` active（`:8800`）。
- 代码/发布线（`~/work/mimir`）：`master @ 9e2ed16`（merge #52 release/1.7.0），工作区有少量本地修改与未跟踪文档/运维目录（见 `git status`）；`feat/1.8.0` 开发线以 `~/work/docs/STATUS.md` 与发布映射文档为准。

## 已接入的对话记忆采集（CDC，实测）
- `hermes-heimdallr-production`（global `~/.hermes/state.db`，owner `heimdallr`）：已采集。
- `hermes-jarvis-profile`（`~/.hermes/profiles/jarvis/state.db`，owner `jarvis`）：已采集，近期持续追平。
- `hermes-quantmaster-profile`（`~/.hermes/profiles/quantmaster/state.db`，owner `quantmaster`）：已采集。
- `hermes-mentor-profile`：历史有采集，近期已停更（库内最后入库较早）。

## 1.8.0 计划增补（本次登记，用户明确要求）
**目标：Thor 与 Odin 的记忆/对话必须被自动记录并可被 Mimir 检索复用。**

### 现状缺口（2026-11-19 盘点）
- **Thor**：未发现 Thor 专属 Hermes profile/state.db 的 CDC 单元；Mimir `conversation_sources` 中无 Thor 对应 `connector_id/owner_principal` 记录，当前未见 Thor 对话被独立、自动采集入库。
- **Odin**：未发现 Odin 的 profile/state.db、CDC 单元或库内 connector/owner 记录，当前未被自动记录。

### 纳入 1.8.0 的工作项（建议拆解）
1. **身份与来源落地**：为 Thor、Odin 明确会话落盘来源（Hermes profile/state.db 或等效可被 CDC 读取的会话存储），分配稳定 `connector_id` / `owner_principal`，避免与 heimdallr/global 混淹。
2. **自动采集链路**：为 Thor/Odin 配置并启用 CDC（或 1.8.0 SessionEnd/`ingest_conversation` 等效路径），纳入 systemd timer 与监控/告警（失败、积压、长时间无新消息可观测）。
3. **自动记忆闭环（对齐 1.8.0 Auto-Capture）**：会话结束自动摄入 → STM 结晶为候选 → 按既有治理/评审策略入库（遵守“自动获取 ≠ 自动入库”；AGG 可自动、UPDATE/DELETE 不自动）→ SessionStart/bootstrap 与 query 可召回。
4. **可观测与验收**：库内可按 owner/connector 查询到 Thor/Odin 的 `conversation_source`/消息与 ingestion 记录；抽取/候选/入库状态可追踪；检索可命中已入库记忆。
5. **文档与运维**：在部署文档/runbook 中登记 Thor/Odin 的 profile、CDC 单元、token/principal 映射与排障步骤。

### 验收标准（可证伪）
- Thor、Odin 各完成至少一轮真实会话后，Mimir 中出现对应 `conversation_sources`/`conversation_messages` 与成功的 ingestion 记录。
- 新会话启动/查询时可检索到对应 agent 已入库的相关记忆（端到端）。
- CDC/采集异常可被监控发现（非静默失败）。

## 风险与注意
- “自动记录/采集”不等于“全部自动入库为长期事实”：入库质量与治理闸门决定可召回内容；需配合 1.8.0 热度/检索接线与必要的显式记忆。
- 生产 systemd 单元存在磁盘定义变更未 reload 的提示，后续涉及 CDC/服务调整时需先出 diff 与回滚方案再按确认流程处理（不在本次执行）。
