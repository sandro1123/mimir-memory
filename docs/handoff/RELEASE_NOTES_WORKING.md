# RELEASE_NOTES_WORKING — 待发布/进行中变更汇总

> 工作稿，随开发更新；发布时据此整理正式 CHANGELOG/Release Notes。

## 1.8.0（规划中）— 自动记忆与采集覆盖
- **[计划增补 2026-11-19] Thor/Odin 记忆自动记录**（用户要求，必须纳入 1.8.0）：
  - 为 Thor、Odin 补齐独立会话来源与 `connector_id/owner_principal`，接入 CDC/自动摄入链路并纳入监控。
  - 打通“采集 → STM 结晶/候选 → 治理入库 → 检索/bootstrap 注入”的端到端闭环（遵守自动获取≠自动入库纪律）。
  - 验收：Thor/Odin 会话可自动采集落库且可被后续会话检索召回；采集/治理失败可观测。
- 其余 1.8.0 范围以 `docs/superpowers/specs/2026-11-12-mimir-1.7-to-2.0-roadmap-design.md` 为准（Auto-Capture/STM-LTM、热度、Adapter 等）。

## 1.7.0
- 以 master/release 记录与已合并 PR 为准（本稿未回填历史条目）。
