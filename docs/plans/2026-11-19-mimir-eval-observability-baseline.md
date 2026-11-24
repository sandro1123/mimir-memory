# Mímir 评测与可观测基线（P1 草案）

> **日期**：2026-11-19  
> **状态**：P1 草案（定义指标、采集点与门禁；部分需配合实现落地）  
> **关联**：`docs/BENCHMARK-PROTOCOL.md`、`docs/BENCHMARK-COMPARE.md`、`docs/INVARIANTS.md`

---

## 1. 目标

为 1.9.x→1.10 提供可证伪的质量与运行基线：**召回质量、摄入/投影健康、适配器归因、治理吞吐、安全审计**均可度量、可告警、可回放。

---

## 2. 评测体系

### 2.1 Golden / 回归（已有）

| 项目 | 当前状态 | 门禁 |
|------|----------|------|
| 金标腿 | `scripts/run_benchmarks.py --golden`，全通道 live `/v8/query` | `eval_suite.py::GOLDEN_FLOORS`：`hit_rate@3 ≥ 0.65`，`hit_rate@10 ≥ 0.83`；floor 被破坏非零退出 |
| 外部基准 | Locomo/LongMemEval 等（本地一次性库） | `engine=fts-only trigram + RRF (no vector lane)` 必须显式标注；degraded/abstain 不计入分母 |

### 2.2 需补齐的评测集与场景

| 集合 | 用途 | 最小要求 |
|------|------|----------|
| **Golden（现有）** | 核心召回回归 | 保持 manifest 指纹与逐案例 `cases[]`；按协议禁止原地覆盖旧编号 |
| **回放集（Replay）** | 真实会话/摄入轨迹回放（脱敏后） | 记录 `session_id`/`idempotency_key`/时间线；可重放验证幂等与 verdict |
| **适配器集（Adapter）** | Claude Code / Codex / Mirasim / Hermes 各路径 | 每 adapter 至少 1 条端到端用例：bootstrap/ingest/query/trace |
| **故障注入集** | 通道失效、账本失败、重复投递 | 覆盖 INV-01~04 场景；断言 degraded 与无副作用 |
| **热度/回声影子集** | Q3/Q4 对照 | 仅 shadow 指标：排序不变，记录 fusion/recall/adopted 计数与漂移 |

### 2.3 A/B 与影子策略

- **热度影响排序**：1.9.0 默认 shadow-only；A/B 前需固定数据集版本与 engine。  
- **自动触发策略**：新 capture 路径先灰度到单 adapter/单 principal，观测漏采与误采后再扩大。  
- **指标对比**：同 commit + 同 manifest + 同 engine；报告必须含 `mimir_version`/`schema_version`/`run_at`。

---

## 3. 可观测维度与指标（建议最小集）

### 3.1 召回漏斗（按请求/会话）

| 指标 | 定义 | 维度标签 |
|------|------|----------|
| `recall.verdict` | found / not_found / degraded 计数与占比 | `adapter`, `host`, `source`, `principal`, `domain` |
| `recall.stage_hits` | 各 funnel stage 命中数/耗时 | `stage`, `adapter` |
| `recall.latency_ms` | p50/p95/p99 | `adapter`, `depth` |
| `recall.candidate_count` | 召回前候选池大小 | `adapter` |
| `recall.empty_but_not_degraded` | **应为 0**（违反 INV-02） | 全局告警 |

### 3.2 摄入与治理

| 指标 | 定义 | 维度标签 |
|------|------|----------|
| `ingest.envelope_total` | 按 connector/session 计数 | `connector_type`, `connector_id`, `principal` |
| `ingest.candidate_created` / `promoted` / `rejected` | 治理吞吐 | `domain`, `fact_type` |
| `ingest.dlp_redacted_total` | DLP 命中次数 | `rule`, `connector_type` |
| `ingest.idempotent_replay` | 重放命中次数 | `adapter` |
| `stm.episode_open/closed/crystallized` | STM 生命周期 | `principal`, `session_scope` |

### 3.3 投影与存储健康

| 指标 | 定义 | 维度标签 |
|------|------|----------|
| `projector.pending` / `dead_letter` | 投影积压与死信 | `projector` |
| `ready.status` | ready / not_ready | 全局（与 `/v8/ready` 对齐） |
| `db.wal_checkpoint` / `size`（可选） | 运维容量信号 | 实例 |

### 3.4 热度与反馈（shadow 阶段）

| 指标 | 定义 | 备注 |
|------|------|------|
| `feedback.signal_total` | useful/useless/correction | 按 `fact_id` 聚合供 evolve；**不直连排序** |
| `heat.fusion_count` / `heat.recall_count` | 双轨计数（待实现） | shadow 字段，不参与 rank |
| `heat.echo_risk` | 高频检索低采纳比率（待定义 adopted） | 告警阈值待实测后设定 |

### 3.5 安全与审计

| 指标 | 定义 | 维度标签 |
|------|------|----------|
| `auth.denied_total` | 401/403/ACL 拒绝 | `code`, `endpoint` |
| `audit.event_total` | 关键事件写入 | `event_type`, `aggregate_type` |
| `egress.blocked_total` | 出口策略拦截（待实现） | `policy`, `destination_class` |

---

## 4. 采集与暴露建议

- **现状**：`/v8/ready`、`/v8/health`、projector status 已存在；trace 可通过 `include_trace`/专用端点获取（部分已在 api）。  
- **P1 目标**：统一结构化日志字段（`trace_id/correlation_id/adapter/host/source/principal/verdict`）；为 Prometheus/OpenTelemetry 预留 exporter 接口（实现可分期）。  
- **最小可用**：先落地 JSON 日志 + 定期聚合脚本，满足 1.9.0 验收观测。

---

## 5. 告警与 SLO（初稿，待实测校准）

| SLO/告警 | 条件 | 动作 |
|----------|------|------|
| Ready | `status != ready` 持续 > 5m | 页警/阻断发布 |
| Dead letter | `dead_letter > 0` | 告警 + 排查 |
| Degraded ratio | 单 adapter `degraded` 占比突增（基线+3σ，待定） | 降级排查，不自动静默 |
| Golden floor | benchmark 非零退出 | CI 阻断 |
| Auth denied spike | 异常突增 | 安全审查 |

---

## 6. 与不变量的映射

- INV-02 → `recall.verdict` 完整性、`empty_but_not_degraded==0`  
- INV-03 → `ingest.idempotent_replay` 与回放集一致性  
- INV-04 → `auth.denied_total` + 审计事件  
- INV-01/06 → 迁移演练与谱系查询回归用例（见 `INVARIANTS.md`）