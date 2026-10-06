"""③-3 时序采集：CDC 必须把会话事件时间带进 envelope（此前恒 NULL）。"""
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mimir_v8.connectors import HermesStateCDC
from mimir_v8.learning import LearningService
from mimir_v8.store import CanonicalStore


def _cdc(store, state_path, connector_id):
    """按真实签名（connectors.py:22）构造 CDC。

    计划 fixture 原写 memory_mode="standard"，实测非法（schema.py:31 合法域
    =explicit/observe/never，"standard" 是 retention_class 的取值）；仅改该
    参数为合法值，断言一字未动。
    """
    return HermesStateCDC(
        store, LearningService(store), state_path,
        connector_id=connector_id, owner_principal="mentor",
        memory_mode="observe", retention_class="standard",
    )


def _run(conn):
    """驱动一次采集——真实入口是 collect_once(actor_principal=...)。

    计划 fixture 原写 conn.run_once()，实测类上无此方法（connectors.py:85 为
    collect_once）；仅改驱动调用，断言一字未动。
    """
    return conn.collect_once(actor_principal="mentor")


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
            # 1791322219.16407 == 2026-10-06T21:30:19.164070+00:00 UTC
            # （计划 brief 原写 1791307819.16407，实测该值 UTC 为 17:30，与断言
            #  要求的 21:30 差整 4 小时；断言不可改，故把常数修正到与断言一致的
            #  真实值，语义不变：两值均为同一 session 的 Unix 浮点秒。）
            self._make_state_db(state, [
                ("s1", "user", "记住：生产库只允许经 API 写入", 1791322200.0),
                ("s1", "assistant", "好的，已记录", 1791322219.16407),
            ])
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conn = _cdc(store, state, "test")
            _run(conn)
            with contextlib.closing(sqlite3.connect(Path(tmp) / "canonical.db")) as c:
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
            _run(conn)
            with contextlib.closing(sqlite3.connect(Path(tmp) / "canonical.db")) as c:
                row = c.execute("SELECT started_at, ended_at FROM conversation_sources").fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row[0], "无时间戳必须留 NULL——解析不了绝不猜")
            self.assertIsNone(row[1])

    def test_out_of_order_timestamps_use_min_max_not_row_order(self):
        """乱序消息：started_at 必须是真最早，不得取分组首条（名不副实）。"""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.db"
            # rowid 顺序：晚(21:30) → 早(17:30) → 中(19:00)
            self._make_state_db(state, [
                ("s3", "user", "晚发的消息", 1791322219.16407),   # 21:30
                ("s3", "user", "早的消息", 1791307819.16407),     # 17:30
                ("s3", "user", "中间的消息", 1791315600.0),       # 19:00
            ])
            store = CanonicalStore(Path(tmp) / "canonical.db")
            _run(_cdc(store, state, "test3"))
            with contextlib.closing(sqlite3.connect(Path(tmp) / "canonical.db")) as c:
                row = c.execute("SELECT started_at, ended_at FROM conversation_sources").fetchone()
            self.assertIn("17:30", row[0], "started_at 必须是真最早（取 min）")
            self.assertIn("21:30", row[1], "ended_at 必须是真最晚（取 max）")

    def test_nan_timestamp_does_not_poison_session_time(self):
        """NaN 会被 min/max 传播成垃圾——必须挡在门外，其余有效值照常。"""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.db"
            self._make_state_db(state, [
                ("s4", "user", "正常消息", 1791307819.16407),
                ("s4", "user", "坏时间戳", float("nan")),
            ])
            store = CanonicalStore(Path(tmp) / "canonical.db")
            _run(_cdc(store, state, "test4"))
            with contextlib.closing(sqlite3.connect(Path(tmp) / "canonical.db")) as c:
                row = c.execute("SELECT started_at, ended_at FROM conversation_sources").fetchone()
            self.assertIsNotNone(row[0], "有效时间戳必须仍被采用")
            self.assertIn("17:30", row[0], "NaN 不得污染——取有效值")
            self.assertNotIn("nan", str(row[0]).lower())
