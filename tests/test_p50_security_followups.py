"""Focused regressions for request-size, federation, and feed parsing boundaries."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.collectors.base import CollectorError
from mimir_v8.collectors.rss import _parse_feed_xml
from mimir_v8.query import QueryKernel
from mimir_v8.store import CanonicalStore


class _Fixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.token = "security-followup-read-token"
        token_path = root / "tokens.json"
        token_path.write_text(json.dumps({"principals": [{
            "id": "mentor",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            "scopes": ["read"], "roles": [],
        }]}), encoding="utf-8")
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(token_path),
            query=QueryKernel(self.store),
        )
        self.client = TestClient(create_app(context), raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {self.token}"}


class TestSecurityFollowups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _Fixture(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_chunked_request_body_over_limit_is_rejected(self):
        def chunks():
            chunk = b"x" * 65536
            for _ in range(20):
                yield chunk

        response = self.fixture.client.post(
            "/v8/learning/remember",
            content=chunks(),
            headers={**self.fixture.auth, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413, response.text)

    def test_federation_query_accepts_read_scope(self):
        response = self.fixture.client.post(
            "/v10/federation/all", json={}, headers=self.fixture.auth,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["count"], 0)

    def test_rss_refuses_doctype_and_entity_declarations(self):
        payload = (
            '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "expanded">]>'
            '<rss><channel><item><title>&x;</title></item></channel></rss>'
        )
        with self.assertRaises(CollectorError):
            _parse_feed_xml(payload)

    def test_rss_without_doctype_still_parses(self):
        root = _parse_feed_xml("<rss><channel><item><title>hello</title></item></channel></rss>")
        self.assertEqual(root.find(".//title").text, "hello")
