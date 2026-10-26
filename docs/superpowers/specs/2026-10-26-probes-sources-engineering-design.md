# Mímir v1.6.0 第二步：声明式探针与信源调度引擎 · 设计文档

> 状态：已实现并通过测试（Step 2 完成）
> 配套计划：`../plans/2026-10-26-probes-sources-engineering-plan.md`

## 1. 目标

为 Mímir 增加"主动抓取外部世界"的触角：

1. **信源登记册** — 声明式 `sources.yaml`，登记免费模型池、GitHub Release 监听器、工具目录。
2. **模型探针引擎** — 轻量 HTTP 探针定期探测免费模型端点，确认其可达性。
3. **断供欠账重放** — 探针失败（超时/不可达）时进入队列，待恢复后手动/定时重放。

探针结果落地为 `entities` 表中的 `model_probe` 类型实体，并与目标 fact
（`free_model_pool` 领域）以 `probe_target` 角色关联。下游"该模型是否断供"
判定可读取 suspended 状态的探针实体。

## 2. 数据模型

### 2.1 复用 Step 1 实体表

- `entities`：存 `model_probe` 类型实体，`name = 模型名`，`domain = free_model_pool`。
- `fact_entities`：建立 `model_probe` 实体 → fact 的 `probe_target` 关联。
- `entity_relations`：预留（模型之间依赖关系尚未启用）。

### 2.2 新增 `probe_tasks` 表（断供欠账重放队列）

```sql
CREATE TABLE IF NOT EXISTS probe_tasks (
    task_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,      -- 失败时的探针结果快照
    status TEXT NOT NULL DEFAULT 'pending',
    due_at TEXT NOT NULL,            -- 该任务可被执行的最早时间（ISO 8601）
    completed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_tasks_due ON probe_tasks(due_at, status);
```

该表在 `SCHEMA_VERSION = 22` 的 `SCHEMA_SQL`（全新库）与
`migrate_schema_21_to_22`（21→22 迁移）中均建立。

## 3. Store API（`mimir_v8/store.py`）

| 方法 | 职责 |
|------|------|
| `probe_model_endpoint(endpoint, timeout=30, *, _opener=None)` | 对端点发送轻量 `GET`，返回 `{"status": "ok"\|"unreachable", "latency_ms": float, "error": str\|None}`。保证在 `timeout` 内返回（超时即 `unreachable`）。`_opener` 仅供测试注入。 |
| `record_model_probe(model_name, domain, outcome, *, fact_id, actor_principal, now=None)` | 将一次探针结果写入 `entities`（ok→`active`，unreachable→`suspended`），并关联 `probe_target` fact，返回 entity_id。 |
| `queue_probe_task(entity_id, endpoint, snapshot, *, due_at, now=None)` | 排队一次失败探针，返回 task_id。 |
| `get_due_probe_tasks(*, now)` | 返回 `now` 之前到期且仍为 `pending` 的任务。 |
| `complete_probe_task(task_id, result, *, now=None)` | 标记任务完成并写回最终探针结果；返回是否有行被更新。 |

`get_entity` 新增 `include_suspended: bool = False` 参数：默认仍排除
`suspended` 实体（与降级/断供标记隔离），但允许下游显式读取 suspended 状态
以判定"模型是否断供"——否则无法区分"已断供"与"不存在"。

## 4. API 边界（`mimir_v8/api.py`）

| 端点 | 鉴权 | 职责 |
|------|------|------|
| `GET /v16/probe/status` | `read` | 汇总 `sources.yaml` 声明的模型及其最新探针状态（active/suspended/none）。 |
| `POST /v16/probe/replay` | `write`（或 admin） | 重放所有到期未完成任务：对每个任务真实再探针一次，结果写回并出队。 |

## 5. 安全考量

- 探针目标是**自己声明的 `sources.yaml`**（可信源），非用户上传 URL，因此复用
  标准 `urllib` 打开器即可；若未来支持用户提供的 URL，应接入 `netsafe.safe_urlopen`
  的 SSRF 防线。
- `probe_model_endpoint` 把所有异常归一为 `unreachable`，避免单点故障抛出未捕获异常。

## 6. 测试覆盖（`tests/`）

- `test_probes_sources_engineering.py`：探针 ok / 不可达 / 超时、结果持久化
  （active / suspended）、队列排队→到期→完成往返。
- `test_probes_api.py`：两个端点的鉴权与行为。

## 7. 与 Step 1 的衔接修复

实现 Step 1 时遗留一处版本不一致：`migrate_schema_21_to_22` 把磁盘
`schema_version` 写为 `"22"`，但运行时常量 `SCHEMA_VERSION` 仍为 `21`，导致
迁移后的库在下一次进程启动时 `_validate_existing_schema` 报错。本步骤一并修复：

- `schema.py`：`SCHEMA_VERSION = 22`。
- `store.py`：`SCHEMA_SQL` 纳入实体三表 + `probe_tasks` 表（全新库直接到 22）。
- `test_entities_migration.py`：改为植入 v21 基线再迁移，验证 21→22。
