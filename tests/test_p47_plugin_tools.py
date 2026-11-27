# -*- coding: utf-8 -*-
"""P47 Hermes memory provider 工具层。

审计 2026-09-07 P1-2 / P1-10：
- 读路径一律用 admin.token → 四 agent 的 owner_only ACL 在召回面被绕过（30 天 admin 1185 次查询）；
- mimir_remember 失败返 {} → agent 被告知「已记住」；
- 幂等键用 Python hash()（每进程随机）→ 网关重启后同内容可重复入库；
- mimir_reflect 发 {"query"}，API 要 {"text"} → 永远 422 静默空。

2026-11-27 Jarvis 隔离：身份从没被赋值 → 所有 profile 都兜底成 mentor，
缺 <owner>.token 时再兜底 admin。现在身份按 profile 显式传入，缺身份/缺
token 一律不发请求（fail closed），不存在任何 mentor/admin 兜底。
tools.py 无 Hermes 依赖，用 importlib 直接加载。
"""
import importlib.util
import os
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

        def fake_post(path, data, owner=None):
            seen["path"] = path
            seen["body"] = data
            seen["owner"] = owner
            return {"results": []}

        with mock.patch.object(t, "_post", side_effect=fake_post):
            t.mimir_reflect("topic x", owner="jarvis")
        self.assertEqual(seen["path"], "/v8/query")
        self.assertEqual(seen["body"], {"text": "topic x", "limit": 10, "depth": "deep"})
        self.assertEqual(seen["owner"], "jarvis")

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

    def test_remember_without_owner_refuses(self):
        t = _load()
        with mock.patch.object(t, "_post") as post:
            out = t.mimir_remember("x")
        self.assertFalse(out["ok"])
        post.assert_not_called()

    def test_search_returns_empty_on_error_dict(self):
        t = _load()
        with mock.patch.object(t, "_post", return_value={"error": {"kind": "http", "status": 401}}):
            self.assertEqual(t.mimir_search("q", owner="jarvis"), [])


class TestPluginIdentity(unittest.TestCase):
    """Jarvis 隔离回归面：身份只来自 profile，绝无 mentor/admin 兜底。"""

    def setUp(self):
        self.t = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "clients"
        self.dir.mkdir()
        (self.dir / "admin.token").write_text("ADMIN\n")
        (self.dir / "mentor.token").write_text("MENTOR\n")
        (self.dir / "jarvis.token").write_text("JARVIS\n")
        self._env = mock.patch.dict(os.environ, {"MIMIR_PLUGIN_TOKEN_DIR": str(self.dir)})
        self._env.start()
        os.environ.pop("MIMIR_PLUGIN_OWNER", None)
        os.environ.pop("MIMIR_PLUGIN_TOKEN_FILE", None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_profile_maps_to_its_own_principal(self):
        self.assertEqual(self.t.resolve_owner("jarvis"), "jarvis")
        self.assertEqual(self.t.resolve_owner("quantmaster"), "quantmaster")
        self.assertEqual(self.t.resolve_owner("default"), "mentor")

    def test_unknown_profile_fails_closed(self):
        for ident in ("custom", "someone-new", "mentor.retired"):
            self.assertIsNone(self.t.resolve_owner(ident), ident)

    def test_env_owner_never_overrides_a_profile(self):
        with mock.patch.dict(os.environ, {"MIMIR_PLUGIN_OWNER": "mentor"}):
            self.assertEqual(self.t.resolve_owner("jarvis"), "jarvis")
            self.assertIsNone(self.t.resolve_owner("custom"))
            self.assertEqual(self.t.resolve_owner(""), "mentor")  # standalone use only

    def test_admin_is_never_an_identity(self):
        with mock.patch.dict(os.environ, {"MIMIR_PLUGIN_OWNER": "admin"}):
            self.assertIsNone(self.t.resolve_owner(""))
        self.assertIsNone(self.t._token("admin"))

    def test_token_is_exactly_the_owner_token(self):
        self.assertEqual(self.t._token("jarvis"), "JARVIS")
        self.assertEqual(self.t._token("mentor"), "MENTOR")

    def test_missing_token_has_no_fallback(self):
        (self.dir / "jarvis.token").unlink()
        self.assertIsNone(self.t._token("jarvis"))  # not MENTOR, not ADMIN
        self.assertIsNone(self.t._token(None))

    def test_no_identity_sends_no_request(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            out = self.t._post("/v8/query", {"text": "q"}, owner=None)
            self.assertEqual(out["error"]["kind"], "no_identity")
            self.assertEqual(self.t.mimir_search("q", owner=None), [])
            urlopen.assert_not_called()

    def test_request_carries_the_owner_token(self):
        seen = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"results": []}'

        def fake_urlopen(req, timeout=10):
            seen["auth"] = req.headers.get("Authorization")
            return _Resp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.t.mimir_search("q", owner="jarvis")
        self.assertEqual(seen["auth"], "Bearer JARVIS")


if __name__ == "__main__":
    unittest.main()
