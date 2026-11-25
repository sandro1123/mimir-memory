# -*- coding: utf-8 -*-
"""1.9.2 security fixes: issues #66-#72 plus the narrowed trading rule."""
from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mimir_v8 import api, mex  # noqa: E402
from mimir_v8.auth import AuthError  # noqa: E402
from mimir_v8.governance import deterministic_check  # noqa: E402
from mimir_v8.learning import _require_owner  # noqa: E402


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "mimir_auto_capture_192", ROOT / "adapters" / "claude-code" / "mimir_auto_capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestHookHostPinning(unittest.TestCase):  # issue #72
    def setUp(self):
        self.hook = _load_hook()
        self.hook.TRUSTED_HOSTS_FILE = Path("/nonexistent/trusted_hosts")

    def test_loopback_is_trusted(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(self.hook.host_is_trusted(host, "8456"))

    def test_arbitrary_host_is_refused(self):
        self.assertFalse(self.hook.host_is_trusted("evil.example.com", "8456"))
        self.assertFalse(self.hook.host_is_trusted("198.51.100.7", "8456"))

    def test_non_numeric_port_is_refused(self):
        self.assertFalse(self.hook.host_is_trusted("127.0.0.1", "8456@evil"))

    def test_listed_host_is_trusted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "trusted_hosts"
            f.write_text("# n100\n198.51.100.7  # odin\n", encoding="utf-8")
            self.hook.TRUSTED_HOSTS_FILE = f
            self.assertTrue(self.hook.host_is_trusted("198.51.100.7", "8456"))
            self.assertFalse(self.hook.host_is_trusted("203.0.113.9", "8456"))


class TestRouteScopes(unittest.TestCase):  # issues #71, #67
    def setUp(self):
        self.src = inspect.getsource(api)

    def test_v7_delete_requires_delete_scope(self):
        block = self.src.split('@app.post("/delete")', 1)[1][:300]
        self.assertIn('scoped("delete")', block)
        self.assertNotIn('scoped("write")', block)

    def test_feedback_checks_read_access(self):
        block = self.src.split('@app.post("/v12/evolve/feedback"', 1)[1][:1500]
        self.assertIn("can_read(", block)


class TestOwnerCheck(unittest.TestCase):  # issue #68
    def test_alias_owner_is_accepted(self):
        _require_owner({"owner_principal": "mentor"}, "heimdallr", "forget")

    def test_other_owner_is_403(self):
        with self.assertRaises(AuthError) as raised:
            _require_owner({"owner_principal": "odin"}, "thor", "correct")
        self.assertEqual(raised.exception.status_code, 403)


class TestMexColumnWhitelist(unittest.TestCase):  # issue #70
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE sources (source_id TEXT, kind TEXT)")

    def test_real_columns_pass(self):
        self.assertEqual(mex._safe_keys(self.conn, "sources", {"source_id": "a", "kind": "b"}),
                         ["source_id", "kind"])

    def test_injected_key_fails(self):
        with self.assertRaises(ValueError):
            mex._safe_keys(self.conn, "sources", {"source_id) VALUES (1); DROP TABLE facts; --": 1})


class TestTradingRuleNarrowed(unittest.TestCase):
    def test_price_chat_is_not_sensitive(self):
        for text in ("京东上这款显卡最低 8890，股票今天大涨", "这笔交易金额不大，下单后看仓位"):
            self.assertFalse(deterministic_check(text)["is_sensitive"], text)

    def test_account_credentials_still_sensitive(self):
        self.assertTrue(deterministic_check("我的资金账号是 1234，交易密码别外传")["is_sensitive"])


if __name__ == "__main__":
    unittest.main()
