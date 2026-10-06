# ③-3 时序采集补齐（Temporal Capture）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Hermes CDC 抽取的会话带上事件时间（started_at/ended_at），使已存在的 Chronos 检索逻辑（过期降权/未生效排后）第一次真正吃到数据。

**Architecture（实测定形，范围比 spec 原文小）:** 检索侧 Chronos 逻辑 **v12 就已完整存在**（`query.py:_decay_factor` 过期×0.5 / `_not_yet_effective` 未来排后 / L0_never 永不过期），写入侧 `learning.py:168` **也已经在传** `env.started_at`。唯一缺口在中间：`connectors.py:120` 构造 `ConversationEnvelope` 时**没填** started_at/ended_at —— 而每条 message 的 `created_at` 已经取到（来自 `messages.timestamp`，Unix 浮点秒）。所以本件=**把已有数据提一层**，检索侧一行不改。

**Tech Stack:** Python 3.11, sqlite3, unittest（`tests/test_*.py`）

**Spec:** `docs/superpowers/specs/2026-10-04-mimir-aidumei-adoption-design.md`（§三 ③-3，范围经实测修正）

## 实测依据（先量后写，2026-10-07）

| 项 | 实测 | 结论 |
|---|---|---|
| `facts.valid_from` | 19/694 | 写入面几乎空 |
| `facts.valid_to` | 0/694 | 从无一条 |
| `conversation_sources.started_at` | **0/7263** | 缺口在此 |
| `conversation_sources.ingested_at` | 7263/7263 | 只有入库时刻 |
| Hermes `sessions.started_at` | 257/257 | 源数据完整 |
| Hermes `messages.timestamp` | 68006/68006 | 源数据完整（Unix 浮点秒） |
| 检索侧 Chronos | 已完整 | **不改** |

**生效范围（诚实声明）**：历史 7263 个 source 的时间无法追溯（ingest 时就没取，源 state.db 的 messages 行也早已滚动）；本件**只向前生效**。不写回填脚本——回填需要按 session_id 重查 state.db，而那批 messages 多数已不在窗口内，强行回填=猜。

## Global Constraints

- **不猜时间**：解析不出就留 NULL（1.2.0 卡二判例「解析不了的时间表达绝不猜成日期」）。
- **检索侧零改动**：`query.py` 一行不动；本件只补采集。
- **向后兼容**：`ConversationEnvelope.started_at` 是可空字段（现有调用方不传仍合法）。
- **时区纪律**：state.db 的 `timestamp` 是 Unix 浮点秒（UTC 语义）；Mímir 存 ISO-8601 UTC 字符串（`utc_now()` 同族）。转换必须显式带 tz。
- TDD：先写失败测试（envelope 带时间），再实现。

---

## File Structure

- **Modify `mimir_v8/connectors.py`** — CDC 分组后从消息时间戳推 session 起止，填进 `ConversationEnvelope`。
- **Modify `mimir_v8/schema.py`**（若需要）— 无（字段已存在）。
- **Create `tests/test_p0r_temporal_capture.py`** — CDC 时间采集的钉。

Non-goals: `query.py` 检索侧、`learning.py`（已在传）、历史回填、`benchmarks_external.py` 的 LOCOMO loader、任何 schema 迁移。

---

### Task 1: CDC 采集 session 事件时间

**Files:**
- Modify: `mimir_v8/connectors.py`（`_ingest_sessions` 内 envelope 构造处 ~120）
- Test: `tests/test_p0r_temporal_capture.py`（新建）

**Interfaces:**
- Consumes: 已取到的 `messages[i]["_created"]`（state.db `timestamp`，Unix 浮点秒字符串形态）
- Produces: `ConversationEnvelope(started_at=<iso8601 UTC>, ended_at=<iso8601 UTC>)` → 经 `learning.ingest_conversation` 落到 `conversation_sources.started_at/ended_at`

**时间转换契约（写进代码注释）：**
- 输入：`str(1791307819.16407)` 或 `None`（列缺失/空）
- 输出：`"2026-10-06T21:30:19.164070+00:00"`（`datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat()`）
- 解析失败/None → `None`（**不猜**）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_p0r_temporal_capture.py
"""③-3 时序采集：CDC 必须把会话事件时间带进 envelope（此前恒 NULL）。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mimir_v8.connectors import HermesStateCDC
from mimir_v8.learning import LearningService
from mimir_v8.store import CanonicalStore


def _cdc(store, state_path, connector_id):
    """按真实签名（connectors.py:22）构造 CDC。"""
    return HermesStateCDC(
        store, LearningService(store), state_path,
        connector_id=connector_id, owner_principal="mentor",
        memory_mode="standard", retention_class="standard",
    )


class TestCdcTemporalCapture(unittest.TestCase):
    def _make_state_db(self, path: Path, rows) -> None:
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, started_at REAL, ended_at REAL)")
        c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
        sessions = {(r[0]) for r in rows}
        for s in sessions:
            c.execute("INSERT INTO sessions(id) VALUES(?)", (s,))
        for sid, role, content, ts in rows:
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                      (sid, role, content, ts))
        c.commit(); c.close()

    def test_session_event_time_lands_in_conversation_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.db"
            # 1791307819.16407 == 2026-10-06T21:30:19.164070+00:00 UTC
            self._make_state_db(state, [
                ("s1", "user", "记住：生产库只允许经 API 写入", 1791307800.0),
                ("s1", "assistant", "好的，已记录", 1791307819.16407),
            ])
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conn = _cdc(store, state, "test")
            conn.run_once()
            with sqlite3.connect(Path(tmp) / "canonical.db") as c:
                row = c.execute(
                    "SELECT started_at, ended_at FROM conversation_sources"
                ).fetchone()
            self.assertIsNotNone(row, "CDC 必须落一条 source")
            self.assertIsNotNone(row[0], "started_at 必须从消息时间戳推出，不得为 NULL")
            self.assertIn("2026-10-06T21:30", row[0], "必须是 UTC ISO-8601")
            self.assertIsNotNone(row[1], "ended_at 必须为最后一条消息时间")

    def test_missing_timestamp_stays_null_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.db"
            self._make_state_db(state, [("s2", "user", "无时间的消息", None)])
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conn = _cdc(store, state, "test2")
            conn.run_once()
            with sqlite3.connect(Path(tmp) / "canonical.db") as c:
                row = c.execute("SELECT started_at, ended_at FROM conversation_sources").fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row[0], "无时间戳必须留 NULL——解析不了绝不猜")
            self.assertIsNone(row[1])
```

**实现者注意**：真实类名=`HermesStateCDC`、构造签名=`(store, LearningService(store), state_path, *, connector_id, owner_principal, memory_mode, retention_class)`（已核实 connectors.py:22）；上面 fixture 已按真实签名写好，断言不得改动。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_p0r_temporal_capture.py -v`
Expected: FAIL — `started_at` 为 None（当前从不填）

- [ ] **Step 3: 实现** — 在 `connectors.py` 的 `for session_id, messages in groups.items():` 块内，envelope 构造前加：

```python
            # ③-3 时序采集：从分组内消息的 timestamp 推 session 事件时间。
            # 源列是 Unix 浮点秒（UTC 语义）；解析不出就留 None——不猜。
            def _ts_to_iso(raw) -> str | None:
                if raw is None:
                    return None
                try:
                    return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
                except (TypeError, ValueError, OSError):
                    return None

            _stamps = [s for s in (_ts_to_iso(item["_created"]) for item in messages) if s]
            started_at = _stamps[0] if _stamps else None
            ended_at = _stamps[-1] if _stamps else None
```

然后在 `ConversationEnvelope(...)` 里加两行：

```python
                started_at=started_at,
                ended_at=ended_at,
```

确认 `connectors.py` 顶部已 import `datetime, timezone`（若没有，加 `from datetime import datetime, timezone`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_p0r_temporal_capture.py -v`
Expected: PASS（2 tests）

- [ ] **Step 5: 回归**（learning/connector 相关全跑）

Run: `python -m pytest tests/ -q -k "connector or learning or ingest or v12_insight" --tb=short`
Expected: 全 PASS（新逻辑是加法，不改变无时间戳时的行为）

- [ ] **Step 6: 提交**

```bash
git add mimir_v8/connectors.py tests/test_p0r_temporal_capture.py
git commit -m "feat(1.3.0-③-3): CDC 采集会话事件时间 — started_at/ended_at 从消息时间戳推出

实测缺口：conversation_sources.started_at 0/7263 填。写入侧 learning.py 早在传
env.started_at，检索侧 Chronos 逻辑 v12 已完整——唯一缺口在 connectors.py 没填。
解析不出留 NULL（1.2.0 卡二判例：不猜日期）。检索侧零改动。"
```

---

## Self-Review

**Spec coverage（§三 ③-3，范围已按实测修正）：**
- spec 原文写「schema 加两列」→ **实测证伪**：`valid_from`/`valid_to` 列 v12 就有，无需迁移。
- spec 原文写「检索融合加时间衰减项」→ **实测证伪**：`_decay_factor` 已有过期×0.5，无需新增。
- spec 原文写「loader 已做出来」→ **实测修正**：卡二 loader 在 `benchmarks_external.py`（基准数据集加载器），**不是生产写入路径**；生产缺的是 CDC 这一层。
- spec 的「开工前先量生产库」纪律 → 已执行，量出真实缺口=7263 个 source 零事件时间。
- 「验收：双轴列守卫式迁移零丢数」→ 不适用（无迁移）；替代验收=新 source 带时间 + 无时间戳留 NULL。

**Placeholder scan:** 无 TBD。实现者须知（类名/构造签名以实际为准）已显式标注，断言不变。

**Type consistency:** `_ts_to_iso` 输入 `float|str|None` 输出 `str|None`；`ConversationEnvelope.started_at: str | None`（learning.py:91 已声明）；落库 `conversation_sources.started_at TEXT`（schema 已有）。

**生效范围诚实声明**：只向前生效，历史 7263 条不追溯（回填=猜，判例禁止）。
