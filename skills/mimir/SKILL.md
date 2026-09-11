---
name: mimir
description: Use when the user mentions Mímir memory system, asks to save/recall long-term memories, review memory candidates, check memory stats/health, or manage skills crystallization. Covers the 27 mimir_* MCP tools.
---

# Mímir 记忆系统操作指南

Mímir 是一个事件溯源的联邦记忆系统（治理管线：候选→评审→提交）。
通过 MCP 工具操作（`mimir_*` 前缀）。生产 API 通常在 `http://127.0.0.1:8456`。

## 什么时候用哪个工具

用户想…… → 用：

| 场景 | 工具 |
|---|---|
| 「帮我记住……」 | `mimir_remember`（content + owner + domain + fact_type） |
| 「你还记得……吗？」「之前说过什么」 | `mimir_query`（text + limit） |
| 深度排查检索质量（漏召回/坏排序） | `mimir_search_trace`（带逐段漏斗） |
| 记住一段完整对话 | `mimir_ingest_conversation` |
| 「忘掉那条」 | `mimir_forget`（tombstone，不是真删） |
| 「之前记错了，应该是……」 | `mimir_correct` |
| 反馈召回结果好坏（有用/没用/纠错） | `mimir_feedback`（检索自进化的燃料） |
| 看系统健康/规模 | `mimir_stats`、资源 `mimir://health` |

## 治理链（Mímir 独有：候选不直接进库）

新记忆先进候选队列，质量评估后人工确认：

`mimir_candidates`（列候选）→ `mimir_review_candidate`（细看）→
`mimir_commit_candidate`（批准入库）或 dismiss。

**写操作走治理链时不该用 `mimir_write` 直写**——那是给已确认事实的。

## 关键纪律

- **owner 语义**：记忆属于谁（agent 身份）就写谁的 owner，不能替别人记（403 owner_boundary）
- **recall_verdict**：查询响应里有 `found / not_found / degraded` 三态——degraded 说明检索通道有故障，别当「没这条记忆」处理
- **忘 ≠ 删**：`mimir_forget` 是 tombstone（可审计），真删除要管理员
- 事实类型速记：`iron_rule`（铁律）/ `user_pref`（用户偏好）/ `project_config` / `event` / `pattern` / `reference`

## 技能结晶（高频模式自动沉淀）

`mimir_crystal_scan`（触发聚类）→ `mimir_crystal_list`（看候选主题）→
`mimir_crystal_approve`（批成技能事实）。
