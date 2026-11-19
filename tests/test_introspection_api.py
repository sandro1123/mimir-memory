# -*- coding: utf-8 -*-
"""1.8.0 spec §4.4/§5.3: additive read-scoped introspection endpoints."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mimir_v8.adapters.registry import list_adapters, manifest as adapter_manifest
from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.query import QueryKernel
from mimir_v8.sources.registry import list_sources, manifest as source_manifest
from mimir_v8.store import CanonicalStore


def _client(tmp_path, *, scopes):
    store = CanonicalStore(tmp_path / "canonical.db")
    token_path = tmp_path / "tokens.json"
    token_path.write_text(json.dumps({
        "principals": [{
            "id": "jarvis",
            "token_sha256": hashlib.sha256(b"tok-jarvis").hexdigest(),
            "scopes": list(scopes),
            "roles": [],
            "admin": False,
        }]
    }), encoding="utf-8")
    ctx = ServiceContext(
        store=store,
        token_store=TokenStore(token_path),
        query=QueryKernel(store),
    )
    return TestClient(create_app(ctx))


class TestAdapterIntrospection(unittest.TestCase):
    def test_adapters_endpoint_requires_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(Path(tmp), scopes=[])
            r = client.get("/v8/hooks/adapters", headers={"Authorization": "Bearer tok-jarvis"})
            self.assertEqual(r.status_code, 403, r.text)

    def test_adapters_endpoint_lists_builtins_non_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(Path(tmp), scopes=("read",))
            r = client.get("/v8/hooks/adapters", headers={"Authorization": "Bearer tok-jarvis"})
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertIn("adapters", data)
            ids = {a["adapter_id"] for a in data["adapters"]}
            self.assertIn("claude-code", ids)
            self.assertIn("codex", ids)
            claude = next(a for a in data["adapters"] if a["adapter_id"] == "claude-code")
            self.assertEqual(claude["hooks"]["end"]["path"], "/v8/hooks/session-end")
            self.assertTrue(claude["capabilities"]["session_end"])
            # No secrets in the payload.
            serialized = json.dumps(data).lower()
            for token in ("token", "secret", "password"):
                self.assertNotIn(token, serialized)

    def test_manifest_helper_matches_endpoint_shape(self):
        expected = [adapter_manifest(s) for s in list_adapters()]
        self.assertTrue(all("adapter_id" in m for m in expected))


class TestSourceConnectorIntrospection(unittest.TestCase):
    def test_connectors_endpoint_requires_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(Path(tmp), scopes=[])
            r = client.get("/v8/sources/connectors", headers={"Authorization": "Bearer tok-jarvis"})
            self.assertEqual(r.status_code, 403, r.text)

    def test_connectors_endpoint_lists_builtins_non_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _client(Path(tmp), scopes=("read",))
            r = client.get("/v8/sources/connectors", headers={"Authorization": "Bearer tok-jarvis"})
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertIn("connectors", data)
            types = {c["connector_type"] for c in data["connectors"]}
            self.assertEqual(
                types,
                {"hermes_cdc", "external_agent", "workbuddy", "rss", "web",
                 "searxng", "feishu", "file", "document", "vault"},
            )
            conv = next(c for c in data["connectors"] if c["connector_type"] == "external_agent")
            self.assertEqual(conv["extraction_policy"], "candidate_only")
            serialized = json.dumps(data).lower()
            for token in ("token", "secret", "password"):
                self.assertNotIn(token, serialized)

    def test_manifest_helper_matches_endpoint_shape(self):
        expected = [source_manifest(s) for s in list_sources()]
        self.assertTrue(all("connector_type" in m for m in expected))


if __name__ == "__main__":
    unittest.main()
