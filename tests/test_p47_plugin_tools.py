# -*- coding: utf-8 -*-
"""P47 Hermes memory provider 工具层。

审计 2026-09-07 P1-2 / P1-10：
- 读路径一律用 admin.token → 四 agent 的 owner_only ACL 在召回面被绕过（30 天 admin 1185 次查询）；
- mimir_remember 失败返 {} → agent 被告知「已记住」；
- 幂等键用 Python hash()（每进程随机）→ 网关重启后同内容可重复入库；
- mimir_reflect 发 {"query"}，API 要 {"text"} → 永远 422 静默空。
tools.py 无 Hermes 依赖，用 importlib 直接加载。
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "hermes-plugin" / "mimir_memory_provider" / "tools.py"


def _load():
    spec = importlib.util.spec_from_file_location("mimir_plugin_tools_p47", str(TOOLS))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPluginTools(unittest.TestCase):
    def test_idempotency_key_is_deterministic(self):
        t = _load()
        self.assertEqual(t._idempotency_key("mentor", "abc"), t._idempotency_key("mentor", "abc"))
        self.assertNotEqual(t._idempotency_key("mentor", "abc"), t._idempotency_key("jarvis", "abc"))
        self.assertTrue(t._idempotency_key("mentor", "abc").startswith("plugin-remember:"))

    def test_reflect_posts_text_field(self):
        t = _load()
        seen = {}

        def fake_post(path, data):
            seen["path"] = path
            seen["body"] = data
            return {"results": []}

        with mock.patch.object(t, "_post", side_effect=fake_post):
            t.mimir_reflect("topic x")
        self.assertEqual(seen["path"], "/v8/query")
        self.assertEqual(seen["body"], {"text": "topic x", "limit": 10})

    def test_remember_surfaces_error(self):
        t = _load()
        with mock.patch.object(t, "_post", return_value={"error": {"kind": "unreachable", "detail": "refused"}}):
            out = t.mimir_remember("x", owner="mentor")
        self.assertFalse(out["ok"])
        self.assertIn("refused", out["error"])

    def test_remember_success_is_marked(self):
        t = _load()
        with mock.patch.object(t, "_post", return_value={"fact_id": "f1", "status": "active"}):
            out = t.mimir_remember("x", owner="mentor")
        self.assertTrue(out["ok"])
        self.assertEqual(out["fact_id"], "f1")

    def test_search_returns_empty_on_error_dict(self):
        t = _load()
        with mock.patch.object(t, "_post", return_value={"error": {"kind": "http", "status": 401}}):
            self.assertEqual(t.mimir_search("q"), [])

    def test_token_prefers_agent_token(self):
        t = _load()
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "clients"
            d.mkdir()
            (d / "admin.token").write_text("ADMIN\n")
            (d / "jarvis.token").write_text("JARVIS\n")
            env = {"MIMIR_PLUGIN_TOKEN_DIR": str(d)}
            with mock.patch.dict("os.environ", env, clear=False), \
                 mock.patch.object(t, "_owner", return_value="jarvis"):
                import os
                os.environ.pop("MIMIR_PLUGIN_TOKEN_FILE", None)
                self.assertEqual(t._token(), "JARVIS")
            with mock.patch.dict("os.environ", env, clear=False), \
                 mock.patch.object(t, "_owner", return_value="mentor"):
                os.environ.pop("MIMIR_PLUGIN_TOKEN_FILE", None)
                self.assertEqual(t._token(), "ADMIN")  # no mentor.token → admin fallback
            env2 = {"MIMIR_PLUGIN_TOKEN_DIR": str(d), "MIMIR_PLUGIN_TOKEN_FILE": str(d / "admin.token")}
            with mock.patch.dict("os.environ", env2, clear=False), \
                 mock.patch.object(t, "_owner", return_value="jarvis"):
                self.assertEqual(t._token(), "ADMIN")  # explicit file wins


if __name__ == "__main__":
    unittest.main()
