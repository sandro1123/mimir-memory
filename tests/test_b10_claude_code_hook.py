# -*- coding: utf-8 -*-
"""Claude Code A-tier hook (adapters/claude-code/mimir_auto_capture.py), 1.9.0.

Pins the three behaviours fixed on 2026-11-24:
- identity is explicit or detected, never borrowed; token path follows identity
- bridge/system scaffolding is stripped before a turn is captured
- resume/compact sessions get the bootstrap, throttled per session
"""
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent.parent / "adapters" / "claude-code" / "mimir_auto_capture.py"


def load_hook(env: dict, home: str):
    clean = {k: v for k, v in os.environ.items()
             if k not in ("MIMIR_AGENT_ID", "MIMIR_TOKEN_FILE", "LARK_CHANNEL", "LARK_CHANNEL_HOME")}
    clean.update(env)
    clean["HOME"] = home
    with mock.patch.dict(os.environ, clean, clear=True):
        spec = importlib.util.spec_from_file_location(f"hook_{time.time_ns()}", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_bridge_without_explicit_id_has_no_identity(self):
        # 1.9.1: LARK_CHANNEL says nothing about which agent this is (Odin runs
        # under the same bridge), so it must not resolve to thor.
        m = load_hook({"LARK_CHANNEL_HOME": "/x", "LARK_CHANNEL": "1"}, self.home)
        self.assertEqual(m.AGENT_ID, "")

    def test_plain_session_has_no_identity(self):
        m = load_hook({}, self.home)
        self.assertEqual(m.AGENT_ID, "")

    def test_explicit_wins(self):
        m = load_hook({"MIMIR_AGENT_ID": "Odin", "LARK_CHANNEL_HOME": "/x"}, self.home)
        self.assertEqual(m.AGENT_ID, "odin")
        self.assertEqual(m.MIMIR_TOKEN_FILE.name, "odin.token")

    def test_missing_token_does_not_borrow(self):
        m = load_hook({"MIMIR_AGENT_ID": "thor"}, self.home)
        self.assertEqual(m._token(), "")  # no thor.token under fake HOME -> nothing

    def test_no_identity_makes_no_network_calls(self):
        m = load_hook({"LARK_CHANNEL_HOME": "/x"}, self.home)
        m.LOG_FILE = Path(self.home) / "hook.log"
        payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup",
                              "session_id": "s1", "cwd": self.home})
        with mock.patch.object(m.urllib.request, "urlopen") as urlopen, \
                mock.patch("sys.stdin", io.StringIO(payload)), \
                redirect_stdout(io.StringIO()) as out:
            m.main()
        urlopen.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertIn("MIMIR_AGENT_ID is not set", m.LOG_FILE.read_text(encoding="utf-8"))


class TestCleaner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.m = load_hook({}, cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_prefers_bridge_user_input_text(self):
        raw = ('[Client tool calling — reminder]\n<bridge_context>\n{"chatId":"oc_x"}\n</bridge_context>\n'
               '<bridge_instructions>["x"]</bridge_instructions>\n<user_input>\n{"text":"继续吧"}\n</user_input>')
        self.assertEqual(self.m.clean_turn_text(raw), "继续吧")

    def test_strips_system_blocks_and_routes(self):
        raw = "<system-reminder>noise</system-reminder>\n真实回答内容 [ACP_ROUTE: <x/>]"
        self.assertEqual(self.m.clean_turn_text(raw), "真实回答内容")

    def test_plain_text_untouched(self):
        self.assertEqual(self.m.clean_turn_text("量化回测必须用真实 PIT 数据"), "量化回测必须用真实 PIT 数据")


class TestResumeInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_hook({"MIMIR_AGENT_ID": "thor", "LARK_CHANNEL_HOME": "/x"}, self.tmp.name)
        self.m.SEEN_FILE = Path(self.tmp.name) / "seen.json"
        self.m.LOG_FILE = Path(self.tmp.name) / "hook.log"
        self.boot = {"iron_rules": [{"content": "PIT 宪法"}], "user_prefs": [{"content": "不要进度提示"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def _start(self, source="resume", sid="s1"):
        buf = io.StringIO()
        with mock.patch.object(self.m, "mimir_get", return_value=self.boot) as get, redirect_stdout(buf):
            self.m.handle_session_start({"source": source, "session_id": sid})
        return get.call_count, buf.getvalue()

    def test_resume_injects_with_additional_context(self):
        calls, out = self._start()
        self.assertEqual(calls, 1)
        payload = json.loads(out)["hookSpecificOutput"]
        self.assertIn("PIT 宪法", payload["additionalContext"])
        self.assertEqual(payload["additionalContext"], payload["injectedPrompt"])

    def test_resume_throttled_per_session(self):
        self._start(sid="s1")
        calls, out = self._start(sid="s1")
        self.assertEqual((calls, out), (0, ""))
        calls, _ = self._start(sid="s2")
        self.assertEqual(calls, 1, "另一个会话不受节流影响")

    def test_fresh_start_always_injects(self):
        self._start(source="startup", sid="s1")
        calls, _ = self._start(source="startup", sid="s1")
        self.assertEqual(calls, 1)


class TestFleetRegistryLine(unittest.TestCase):
    INFO = ("fleet 0.1.0 · registry /r · repo /r\n"
            "commit 5046d2f（2026-11-27T09:42:54+00:00） · 同步于 0.6 小时前\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load_hook({"MIMIR_AGENT_ID": "thor", "LARK_CHANNEL_HOME": "/x"}, self.tmp.name)
        self.m.SEEN_FILE = Path(self.tmp.name) / "seen.json"
        self.m.LOG_FILE = Path(self.tmp.name) / "hook.log"

    def tearDown(self):
        self.tmp.cleanup()

    def _line(self, which="/usr/bin/fleet", stdout="", exc=None):
        run = mock.Mock(side_effect=exc) if exc else mock.Mock(return_value=mock.Mock(stdout=stdout))
        with mock.patch.object(self.m.shutil, "which", return_value=which), \
                mock.patch.object(self.m.subprocess, "run", run):
            return self.m.fleet_registry_line()

    def test_parses_commit_and_time(self):
        line = self._line(stdout=self.INFO)
        self.assertIn("commit 5046d2f（2026-11-27T09:42:54+00:00）", line)
        self.assertIn("fleet pointer thor", line)

    def test_empty_agent_id_no_dangling_space(self):
        with mock.patch.object(self.m, "AGENT_ID", ""):
            line = self._line(stdout=self.INFO)
        self.assertIn("以 fleet pointer 为准", line)

    def test_no_fleet_no_line(self):
        self.assertEqual(self._line(which=None), "")

    def test_timeout_no_line(self):
        self.assertEqual(self._line(exc=self.m.subprocess.TimeoutExpired("fleet", 3)), "")

    def test_unparsable_no_line(self):
        self.assertEqual(self._line(stdout="something else"), "")

    def test_line_survives_truncation(self):
        boot = {"iron_rules": [{"content": "x" * 140}] * 6, "user_prefs": [{"content": "y" * 130}] * 5}
        buf = io.StringIO()
        with mock.patch.object(self.m, "mimir_get", return_value=boot), \
                mock.patch.object(self.m, "fleet_registry_line", return_value="■ 舰队注册表：commit abc1234"), \
                mock.patch.object(self.m, "INJECT_MAX_CHARS", 200), redirect_stdout(buf):
            self.m.handle_session_start({"source": "startup", "session_id": "s"})
        ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.endswith("…\n■ 舰队注册表：commit abc1234"))


if __name__ == "__main__":
    unittest.main()
