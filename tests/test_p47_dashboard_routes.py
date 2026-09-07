# -*- coding: utf-8 -*-
"""P47 看板路由 / 缓存失效 / 会话密钥 / 前端错误可见性。

审计 2026-09-07 P1-9 / P1-12：
- `/v11/symbolic/offload` 装饰器被同一行注释吞掉，从未注册（前端在调 → 404）；
- `GET /v10/opinions|observations` 误装饰到私有 helper `_mimir_get_params`，成为两条假路由；
- review 端点接受 GET 触发副作用；
- conflicts 端点失效一个不存在的缓存键，crystals / governance-run 零失效；
- 密码文件不可读时 HMAC 密钥退化为公开常量 → 可伪造 cookie；
- 前端 fetchJSON 三路返 {} 无任何用户可见信号。
"""
import hashlib
import hmac
import importlib.util
import inspect
import os
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "dashboard" / "backend" / "main.py"
INDEX = ROOT / "dashboard" / "frontend" / "index.html"


def _load():
    spec = importlib.util.spec_from_file_location("dash_main_p47", str(MAIN))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.routes = {r.path: r for r in self.mod.app.routes if hasattr(r, "methods")}

    def test_v11_offload_registered(self):
        self.assertIn("/v11/symbolic/offload", self.routes)
        self.assertEqual(self.routes["/v11/symbolic/offload"].methods, {"POST"})

    def test_helper_not_exposed_as_route(self):
        self.assertNotIn("/v10/opinions", self.routes)
        self.assertNotIn("/v10/observations", self.routes)

    def test_review_is_post_only(self):
        self.assertEqual(self.routes["/api/candidates/{candidate_id}/review"].methods, {"POST"})

    def test_frontend_dir_resolves_from_backend_layout(self):
        env = {k: v for k, v in os.environ.items() if k != "FRONTEND_DIR"}
        with mock.patch.dict("os.environ", env, clear=True):
            mod = _load()
        self.assertTrue((mod.FRONTEND_DIR / "index.html").exists(), mod.FRONTEND_DIR)


class TestCacheInvalidation(unittest.TestCase):
    WRITE_ENDPOINTS = (
        "api_review_candidate", "api_commit_candidate", "api_conflict_resolve",
        "api_conflict_dismiss", "api_crystals_scan", "api_crystals_approve",
        "api_crystals_dismiss", "api_governance_run", "api_skills_promote",
    )

    def test_invalidate_all_clears_everything(self):
        mod = _load()
        mod._cache.update({"facts": (0, 1), "dash_today:x=1": (0, 2), "skills": (0, 3)})
        mod._invalidate_all()
        self.assertEqual(mod._cache, {})

    def test_every_write_endpoint_invalidates_all(self):
        mod = _load()
        for name in self.WRITE_ENDPOINTS:
            src = inspect.getsource(getattr(mod, name))
            self.assertIn("_invalidate_all()", src, name)


class TestSessionSecret(unittest.TestCase):
    def test_empty_password_hash_does_not_yield_public_secret(self):
        mod = _load()
        with mock.patch.object(mod, "_stored_password_hash", return_value=""):
            expiry = str(int(time.time()) + 3600)
            public_secret = hashlib.sha256(b"" + b"mimir-dashboard-session-v1").digest()
            forged = f"{expiry}.{hmac.new(public_secret, expiry.encode(), hashlib.sha256).hexdigest()}"
            self.assertFalse(mod._valid_session_token(forged))
            self.assertTrue(mod._valid_session_token(mod._make_session_token()))


class TestFrontendErrorVisibility(unittest.TestCase):
    def test_fetchjson_reports_errors(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("netErr", html)
        self.assertNotIn("if (!r.ok) return {};", html)


if __name__ == "__main__":
    unittest.main()
