# -*- coding: utf-8 -*-
"""Claude Code hook: latest-turn extraction past large tool results (#81), 1.9.6.

The hook used to read only the last 300 KB of the transcript, so one large
trailing tool_result hid the real user/assistant turn and capture came back
empty. Pins: bounded reverse chunked scan, oversized records skipped, the
newest user turn is never paired with an older reply, and a failed scan says
why instead of looking like a short turn.
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent.parent / "adapters" / "claude-code" / "mimir_auto_capture.py"


def load_hook(home: str):
    clean = {k: v for k, v in os.environ.items()
             if k not in ("MIMIR_AGENT_ID", "MIMIR_TOKEN_FILE", "LARK_CHANNEL", "LARK_CHANNEL_HOME")}
    clean["HOME"] = home
    with mock.patch.dict(os.environ, clean, clear=True):
        spec = importlib.util.spec_from_file_location(f"hook_{time.time_ns()}", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def user(text):
    return {"type": "user", "message": {"content": text}}


def assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool_result(size):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "synthetic", "content": "x" * size}]}}


class TestExtractLatestTurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_hook(self.tmp.name)
        self.logs = []
        patcher = mock.patch.object(self.m, "log", self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, raw_lines=()):
        p = Path(self.tmp.name) / f"t{time.time_ns()}.jsonl"
        lines = [json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r for r in rows]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def test_plain_turn(self):
        t = self.write([user("Only inspect the current task."), assistant("Inspection is in progress.")])
        self.assertEqual(self.m.extract_latest_turn(t),
                         ("Only inspect the current task.", "Inspection is in progress."))

    def test_trailing_900k_tool_result(self):
        t = self.write([user("Only inspect the current task."), assistant("Inspection is in progress."),
                        tool_result(900_000)])
        self.assertEqual(self.m.extract_latest_turn(t),
                         ("Only inspect the current task.", "Inspection is in progress."))

    def test_trailing_4mb_tool_result(self):
        t = self.write([user("Only inspect the current task."), assistant("Inspection is in progress."),
                        tool_result(4_000_000)])
        self.assertEqual(self.m.extract_latest_turn(t),
                         ("Only inspect the current task.", "Inspection is in progress."))

    def test_cjk_and_many_tool_results_across_tiny_chunks(self):
        self.m._TAIL_CHUNK_BYTES = 7  # forces multi-byte characters onto chunk boundaries
        t = self.write([user("旧问题"), assistant("旧回答"),
                        user("请检查记忆系统的状态"), assistant("记忆系统运行正常，版本一致。"),
                        tool_result(300), tool_result(500), tool_result(200)])
        self.assertEqual(self.m.extract_latest_turn(t),
                         ("请检查记忆系统的状态", "记忆系统运行正常，版本一致。"))

    def test_latest_user_without_reply_is_not_paired_with_older_reply(self):
        t = self.write([user("第一个问题"), assistant("第一个回答"), user("第二个问题")])
        self.assertEqual(self.m.extract_latest_turn(t), ("第二个问题", ""))

    def test_latest_user_uses_fallback_reply(self):
        t = self.write([user("第一个问题"), assistant("第一个回答"), user("第二个问题")])
        self.assertEqual(self.m.extract_latest_turn(t, "当前回答"), ("第二个问题", "当前回答"))

    def test_malformed_lines_and_non_human_records_skipped(self):
        t = self.write([
            user("真实的问题"), assistant("真实的回答"),
            '{"type": "user", "message": {"content": "broken',
            "not json at all",
            {"type": "user", "isMeta": True, "message": {"content": "Caveat: injected by the harness"}},
            user("<task-notification><task-id>b1</task-id><status>completed</status></task-notification>"),
        ])
        self.assertEqual(self.m.extract_latest_turn(t), ("真实的问题", "真实的回答"))

    def test_scan_limit_returns_empty_and_logs_reason(self):
        self.m._TAIL_CHUNK_BYTES = 256
        self.m._TAIL_MAX_SCAN_BYTES = 2_000
        t = self.write([user("很早的问题"), assistant("很早的回答")] + [tool_result(400) for _ in range(20)])
        self.assertEqual(self.m.extract_latest_turn(t), ("", ""))
        self.assertTrue(any("scan limit" in s for s in self.logs), self.logs)

    def test_oversized_record_is_skipped_not_buffered(self):
        self.m._TAIL_CHUNK_BYTES = 256
        self.m._TAIL_MAX_RECORD_BYTES = 1_000
        t = self.write([user("Only inspect the current task."), assistant("Inspection is in progress."),
                        tool_result(50_000)])
        self.assertEqual(self.m.extract_latest_turn(t),
                         ("Only inspect the current task.", "Inspection is in progress."))
        self.assertTrue(any("oversized" in s for s in self.logs), self.logs)

    def test_missing_file(self):
        self.assertEqual(self.m.extract_latest_turn("/nonexistent/t.jsonl", "fb"), ("", "fb"))


if __name__ == "__main__":
    unittest.main()
