# #34 钩子 — SessionEnd 自动结晶（1.8.0 的 A 档子集）

> 2026-11-13 · 状态：Design（待人工批准前不合并代码）
> 上游：路线图 §5.2（A 档钩子）+ §5.1（STM 纪律）

## 1. 现状（实测）

- `POST /v8/ingestion/conversations` 已有（`api.py:1559`），`memory_mode ∈ {explicit, observe, never}`（`schema.py:33`）。
- `ingest_conversation` 只写 `conversation_sources` + `conversation_messages` + `ingestion_runs(status=stored)`；**不产 candidate_facts**。
- 候选逻辑在 `learning.remember()`（显式，已有 `explicit_memory_requires_review`）；`candidate_facts(cand_id/content/summary/owner/domain/fact_type/source_id/source_hash/visibility/egress/uncertainty_reasons/idempotency_key)` 结构齐备。
- `proactive_wake` 已有 API（`api.py:1097`），但无 SessionStart 预取钩子。
- 本机空库（`.hermes/mimir/data/canonical.db` facts=0），生产在 n100。

## 2. 本次只做①（不做②③）

按路线图 §13-4 “#34+A 钩子”是单步，拆成三件：

| | 交付 | 本次 |
|---|---|---|
| ① | `POST /v8/hooks/session-end` — SessionEnd 时自动结晶一条候选（含 provenance） | ✅ |
| ② | SessionStart 预取钩子（`/v15/bootstrap` L0/L1 注入） | 留待后续，该端点已有 `proactive_wake` |
| ③ | PostToolUse trajectory 摘要 | 不做（工具调用摘要不进库，见 §5.2） |

### ① 的设计

- 入参：`HookSessionEnd`（`connector_type/connector_id/session_id/owner_principal/messages[]/idempotency_key`）与现 `ConversationEnvelope` 平行，但 `memory_mode` 资源不可配置——**自动结晶要求 `memory_mode ∈ {observe, never}`，`explicit` 拒绝**（显式档已有 `learning.remember` 的正确路径，走这条会绕评审）。
- 动作（同一事务）：
  1. 走 `ingest_conversation` 优雅的 DLP + fingerprint + 幂等逻辑（复用，不重写）。
  2. 跑 Tier-0/1/2 判重（现 candidates pipeline）出候选：`proposed_fact_type` 待定 → 用现有 classifier 或 observe 通道；`uncertainty_reasons=("auto_captured_requires_review",)`；`proposed_egress_policy="local_only"`（自动捕获默认不外发）。
  3. 幂等键：`{session_id}:{conversation_id}:hook-end`，重放不会再产候选。
- API 契约：不修改现有 `POST /v8/ingestion/conversations`；新端点 `POST /v8/hooks/session-end`，`200 <same shape> + candidate_id`。
- **STM 纪律**：会话结束时自动结晶出候选（AGG 全自动），不是直接写 facts。不引入新的 `session_episode` 表（`conversation_sources` 即是会话作用域存储）。

### 验收（可证伪）

| 项 | 判据 |
|---|---|
| A 档端到端 | Claude Code 实机一轮会话后 STM（`conversation_sources`）+ 候选（`candidate_facts`）齐现，零 facts 写入 |
| 幂等 | 同一幂等键重放返回同一 `candidate_id`，无重复候选 |
| memory_mode | `explicit` 拒 422；`observe`/`never` 通过 |
| 无评审绕过 | facts 表检查 0 写入 |
| DLP | 明文密钥在候选内容里已脱敏（复用 `_SECRET_PATTERNS`） |

### ③ 不做的理由

- PostToolUse 轨迹属于 ephemeral 执行日志，进库会产生“每 tool 一条候选”的洪泛；轨迹摘要应作为一个有上限的列表一次性产 1 条，或不入库。

## 3. 风险前置

- `candidates` 的 fact_type 推断（classifier）质量未知——§5 上线前先在 BENCHMARK-PROTOCOL 下半日跑数。
- memory_mode 三值 `explicit/observe/never` 语义曾在 CHANGELOG 演过（`observe` 曾叫 `auto`），本文沿现行三值。

---
*本计划 approval 后代码从此 plan 推进，不在此文档里写实现。*
