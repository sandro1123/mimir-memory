"""Mirasim chat-log collector (adapters/mirasim/collector.py).

Design: docs/plans/2026-11-25-mirasim-capture-design.md. Uses fake room logs in
the real Mirasim layout; nothing touches the network.
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "adapters" / "mirasim" / "collector.py"
_spec = importlib.util.spec_from_file_location("mirasim_collector", _PATH)
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def _ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _msg(seq, minutes_ago, *, by="usr_1", text="hello", **extra):
    return {"v": 1, "seq": seq, "ts": _ts(minutes_ago), "by": by, "name": by,
            "kind": "message", "text": text, **extra}


class Chat:
    """A temporary ``chat`` dir with ``rooms/<room>/log/*.jsonl`` and ``users``."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for uid, kind in (("usr_1", "human"), ("ai-matrix", "agent")):
            d = self.root / "users" / uid
            d.mkdir(parents=True)
            (d / "profile.json").write_text(json.dumps({"kind": kind}), encoding="utf-8")

    def room(self, name, rows, first=1):
        log = self.root / "rooms" / name / "log"
        log.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"v": 1, "seq": 0, "kind": "room"})] + [json.dumps(r, ensure_ascii=False) for r in rows]
        (log / f"{first:08d}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def close(self):
        self._tmp.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.chat = Chat()
        self.addCleanup(self.chat.close)
        self.sent = []

    def run_collect(self, cursor, *, fail=False, dry_run=False):
        def send(env):
            if fail:
                raise ConnectionError("api down")
            self.sent.append(env)
        return mc.collect(self.chat.root, cursor, now=NOW, send=send, dry_run=dry_run)


class TestRoomsAndKinds(Base):
    def test_room_filter(self):
        self.assertTrue(mc.room_included("dm-ai-matrix+usr_1"))
        self.assertTrue(mc.room_included("dm-mx-matrix+usr_1"))
        self.assertTrue(mc.room_included("general"))
        self.assertFalse(mc.room_included("items"))

    def test_only_message_lines_and_roles(self):
        self.chat.room("dm-ai-matrix+usr_1", [
            _msg(1, 60, text="问题"),
            {"v": 1, "seq": 2, "ts": _ts(59), "kind": "system", "text": "joined"},
            {"v": 1, "seq": 3, "ts": _ts(58), "kind": "react", "target": 1},
            _msg(4, 57, by="ai-matrix", text="回答"),
        ])
        self.chat.room("items", [_msg(1, 60, text="todo")])
        cursor = {}
        report = self.run_collect(cursor)
        self.assertEqual(len(self.sent), 1)
        msgs = self.sent[0]["messages"]
        self.assertEqual([(m["role"], m["content"]) for m in msgs], [("user", "问题"), ("assistant", "回答")])
        self.assertEqual(self.sent[0]["owner_principal"], "mirasim")
        self.assertEqual(self.sent[0]["connector_id"], "mirasim-x1e")
        self.assertEqual(self.sent[0]["idempotency_key"], "mirasim:dm-ai-matrix+usr_1:1-4")
        self.assertEqual(cursor, {"dm-ai-matrix+usr_1": 4})
        self.assertNotIn("items", report["rooms"])


class TestCursorAndCooldown(Base):
    def test_cursor_skips_sent_and_is_idempotent(self):
        self.chat.room("general", [_msg(1, 60), _msg(2, 50), _msg(3, 40)])
        cursor = {"general": 2}
        self.run_collect(cursor)
        self.assertEqual([m["metadata"]["seq"] for m in self.sent[0]["messages"]], [3])
        self.sent.clear()
        self.run_collect(cursor)
        self.assertEqual(self.sent, [])

    def test_cooldown_stops_before_recent_messages(self):
        self.chat.room("general", [_msg(1, 30), _msg(2, 5), _msg(3, 20)])
        cursor = {}
        self.run_collect(cursor)
        # seq 2 is inside the 10-minute cooldown: stop there, even though seq 3 is older.
        self.assertEqual([m["metadata"]["seq"] for m in self.sent[0]["messages"]], [1])
        self.assertEqual(cursor, {"general": 1})

    def test_backfill_window_on_first_run_only(self):
        self.chat.room("general", [_msg(1, 60 * 24 * 10), _msg(2, 60)])
        cursor = {}
        report = self.run_collect(cursor)
        self.assertEqual([m["metadata"]["seq"] for m in self.sent[0]["messages"]], [2])
        self.assertEqual(report["rooms"]["general"]["skipped_backfill"], 1)
        self.assertEqual(cursor, {"general": 2})

    def test_failed_send_keeps_cursor(self):
        self.chat.room("general", [_msg(1, 60)])
        cursor = {"general": 0}
        report = self.run_collect(cursor, fail=True)
        self.assertEqual(cursor, {"general": 0})
        self.assertEqual(len(report["failed"]), 1)

    def test_dry_run_sends_nothing_and_keeps_cursor(self):
        self.chat.room("general", [_msg(1, 60)])
        cursor = {}
        report = self.run_collect(cursor, dry_run=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(cursor, {})
        self.assertEqual(report["sent_messages"], 1)


class TestContent(Base):
    def test_edit_applied_card_text_and_fail_skipped(self):
        self.chat.room("dm-ai-matrix+usr_1", [
            _msg(1, 60, by="ai-matrix", text="draft"),
            {"v": 1, "seq": 2, "ts": _ts(59), "kind": "edit", "target": 1, "text": "final"},
            _msg(3, 58, by="ai-matrix", text="", card={"type": "dispatch", "title": "审计任务"}),
            _msg(4, 57, by="ai-matrix", text="Couldn't finish", cid="assist:fail:1:1"),
        ])
        cursor = {}
        report = self.run_collect(cursor)
        msgs = self.sent[0]["messages"]
        self.assertEqual([m["content"] for m in msgs], ["final", "审计任务"])
        self.assertTrue(msgs[0]["metadata"]["edited"])
        self.assertEqual(report["rooms"]["dm-ai-matrix+usr_1"]["skipped_fail"], 1)
        self.assertEqual(cursor, {"dm-ai-matrix+usr_1": 4})

    def test_batches_split_and_cover_contiguous_seqs(self):
        rows = [_msg(i, 300 - i) for i in range(1, mc.BATCH_SIZE + 6)]
        self.chat.room("general", rows)
        cursor = {}
        self.run_collect(cursor)
        self.assertEqual([len(e["messages"]) for e in self.sent], [mc.BATCH_SIZE, 5])
        self.assertEqual(self.sent[0]["idempotency_key"], f"mirasim:general:1-{mc.BATCH_SIZE}")
        self.assertEqual(self.sent[1]["idempotency_key"],
                         f"mirasim:general:{mc.BATCH_SIZE + 1}-{mc.BATCH_SIZE + 5}")


if __name__ == "__main__":
    unittest.main()
