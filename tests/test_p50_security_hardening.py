# -*- coding: utf-8 -*-
"""P50 安全加固回归测试（2026-10-26 全量审计）。

覆盖：
- netsafe：SSRF 校验（私网/回环/元数据地址、IPv4-mapped IPv6、重定向逐跳复检、读取上限、显式放行开关）；
- RSS：拒绝 DOCTYPE/ENTITY（billion-laughs / XXE）；
- crawler：不再绕过 SSRF 校验；
- TokenStore：同一 mtime 下的两次原子写也必须触发重载（吊销即时生效）；
- API：X-Mimir-Agent 仅认证后注册；chunked 请求同样受 1 MiB 限制；federation 只需 read scope；
- Dashboard：scrypt 加盐密码、兼容旧 SHA-256、登出吊销会话、/api/source/add 不再出站抓取。
"""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from mimir_v8 import netsafe
from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore, atomic_write_registry
from mimir_v8.collectors.crawler import WebCrawler
from mimir_v8.collectors.base import CollectorError
from mimir_v8.collectors.rss import _parse_feed_xml
from mimir_v8.query import QueryKernel
from mimir_v8.schema import get_registered_agents
from mimir_v8.store import CanonicalStore

ROOT = Path(__file__).resolve().parents[1]


def _ip(*octets: int) -> str:
    """Build private IPs at runtime so scripts/release_scan.py's literal
    private-IP gate stays meaningful for real leaks."""
    return ".".join(str(o) for o in octets)


class TestNetsafeValidate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("MIMIR_ALLOW_PRIVATE_FETCH", None)

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x"):
            with self.assertRaises(netsafe.UnsafeURLError):
                netsafe.validate_url(url)

    def test_rejects_private_and_metadata_literals(self):
        for url in (
            "http://127.0.0.1/", f"http://{_ip(10, 0, 0, 1)}/", f"http://{_ip(192, 168, 5, 5)}/",
            "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
            "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://localhost:8456/",
            "http://100.64.0.1/",
        ):
            with self.subTest(url=url), self.assertRaises(netsafe.UnsafeURLError):
                netsafe.validate_url(url)

    def test_rejects_hostname_resolving_to_private(self):
        fake = [(2, 1, 6, "", (_ip(10, 1, 2, 3), 0))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            with self.assertRaises(netsafe.UnsafeURLError):
                netsafe.validate_url("http://internal.example/")

    def test_allows_public_address(self):
        fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            netsafe.validate_url("https://example.com/feed.xml")

    def test_opt_in_allows_private(self):
        with mock.patch.dict(os.environ, {"MIMIR_ALLOW_PRIVATE_FETCH": "1"}):
            netsafe.validate_url(f"http://{_ip(192, 168, 1, 10)}/rss")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = b"x" * 5000
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestNetsafeFetch(unittest.TestCase):
    """Run a loopback server; loopback is allowed for the first hop only via
    the opt-in switch, so we patch validate_url to permit 127.0.0.1 but still
    apply the real policy to every other host (i.e. the redirect target)."""

    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        cls._env.start()
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls._env.stop()

    def _validate_except_loopback(self, real):
        def inner(url):
            if url.startswith(f"http://127.0.0.1:{self.port}/"):
                return None
            return real(url)
        return inner

    def test_redirect_to_metadata_is_blocked(self):
        real = netsafe.validate_url
        with mock.patch.object(netsafe, "validate_url", self._validate_except_loopback(real)):
            with self.assertRaises(netsafe.UnsafeURLError):
                netsafe.safe_urlopen(f"http://127.0.0.1:{self.port}/redirect-private", timeout=5)

    def test_body_is_capped(self):
        real = netsafe.validate_url
        with mock.patch.object(netsafe, "validate_url", self._validate_except_loopback(real)):
            res = netsafe.safe_urlopen(f"http://127.0.0.1:{self.port}/big", timeout=5, max_bytes=1000)
        self.assertEqual(len(res.body), 1000)
        self.assertTrue(res.truncated)


class TestCollectors(unittest.TestCase):
    def test_rss_refuses_doctype(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                '<!ENTITY lol2 "&lol;&lol;&lol;">]><rss><channel><item>'
                '<title>&lol2;</title></item></channel></rss>')
        with self.assertRaises(CollectorError):
            _parse_feed_xml(bomb)

    def test_rss_plain_feed_parses(self):
        root = _parse_feed_xml("<rss><channel><item><title>t</title></item></channel></rss>")
        self.assertEqual(root.find(".//title").text, "t")

    def test_crawler_blocks_private_targets(self):
        os.environ.pop("MIMIR_ALLOW_PRIVATE_FETCH", None)
        self.assertIsNone(WebCrawler()._fetch_page("http://127.0.0.1:1/"))


class TestTokenStoreReload(unittest.TestCase):
    def test_same_mtime_rewrite_still_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"

            def write(ids):
                data = {"principals": [
                    {"id": i, "token_sha256": hashlib.sha256(f"t-{i}".encode()).hexdigest(),
                     "scopes": ["read"], "roles": []} for i in ids]}
                atomic_write_registry(path, json.dumps(data))

            write(["a", "b"])
            store = TokenStore(path)
            self.assertEqual(store.validate(), 2)
            mtime = path.stat().st_mtime_ns
            write(["a"])  # revoke b
            os.utime(path, ns=(mtime, mtime))  # force identical mtime
            self.assertEqual(store.validate(), 1)


class _APIFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.token = "p50-token"
        token_path = root / "tokens.json"
        token_path.write_text(json.dumps({"principals": [{
            "id": "mentor",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            "scopes": ["read"], "roles": [],
        }]}), encoding="utf-8")
        ctx = ServiceContext(store=self.store, token_store=TokenStore(token_path),
                             query=QueryKernel(self.store))
        self.client = TestClient(create_app(ctx), raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {self.token}"}


class TestAPIHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = _APIFixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_anonymous_agent_header_is_not_registered(self):
        name = "p50-anon-agent"
        self.fx.client.get("/health", headers={"X-Mimir-Agent": name})
        self.assertNotIn(name, get_registered_agents())

    def test_authenticated_agent_header_is_registered(self):
        name = "p50-authed-agent"
        self.fx.client.get("/health", headers={"X-Mimir-Agent": name, **self.fx.auth})
        self.assertIn(name, get_registered_agents())

    def test_malformed_agent_id_is_ignored(self):
        name = "bad agent/../x"
        self.fx.client.get("/health", headers={"X-Mimir-Agent": name, **self.fx.auth})
        self.assertNotIn(name.lower(), get_registered_agents())

    def test_chunked_body_over_limit_is_rejected(self):
        def gen():
            chunk = b"a" * 65536
            for _ in range(20):  # 1.25 MiB, no Content-Length
                yield chunk
        resp = self.fx.client.post("/v8/learning/remember", content=gen(),
                                   headers={**self.fx.auth, "Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 413)

    def test_federation_needs_only_read_scope(self):
        resp = self.fx.client.post("/v10/federation/all", json={}, headers=self.fx.auth)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("Traceback", resp.text)


def _load_dashboard():
    spec = importlib.util.spec_from_file_location("dash_main_p50", str(ROOT / "dashboard/backend/main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDashboardAuth(unittest.TestCase):
    def setUp(self):
        self.mod = _load_dashboard()

    def test_scrypt_roundtrip_and_salted(self):
        h1, h2 = self.mod.hash_password("pw"), self.mod.hash_password("pw")
        self.assertTrue(h1.startswith("scrypt$"))
        self.assertNotEqual(h1, h2)
        self.assertTrue(self.mod._verify_password("pw", h1))
        self.assertFalse(self.mod._verify_password("nope", h1))

    def test_legacy_sha256_still_verifies(self):
        legacy = hashlib.sha256(b"pw").hexdigest()
        self.assertTrue(self.mod._verify_password("pw", legacy))
        self.assertFalse(self.mod._verify_password("", legacy))

    def test_logout_revokes_session(self):
        with mock.patch.object(self.mod, "_stored_password_hash", return_value=self.mod.hash_password("pw")):
            tok = self.mod._make_session_token()
            self.assertTrue(self.mod._valid_session_token(tok))
            self.mod._revoke_session(tok)
            self.assertFalse(self.mod._valid_session_token(tok))

    def test_source_add_makes_no_outbound_request(self):
        import inspect
        src = inspect.getsource(self.mod.api_source_add)
        self.assertNotIn("urlopen", src)


if __name__ == "__main__":
    unittest.main()
