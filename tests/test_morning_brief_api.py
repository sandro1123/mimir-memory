# -*- coding: utf-8 -*-
"""API tests for v1.6.0 Step 3 morning-brief / double-layer crystallization endpoint."""
import sys
import tempfile
import json
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from mimir_v8.api import create_app, ServiceContext
from mimir_v8.store import CanonicalStore
from mimir_v8.query import QueryKernel
from mimir_v8.auth import TokenStore


def _make_client(scopes, admin=False):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    store = CanonicalStore(root / "canon.db")
    store.migrate_schema_21_to_22()
    token_path = root / "tokens.json"
    token_path.write_text(json.dumps({
        "principals": [{
            "id": "mentor",
            "token_sha256": hashlib.sha256("tok-brief".encode()).hexdigest(),
            "scopes": scopes,
            "roles": ["authenticated"],
            "admin": admin,
        }]
    }), encoding="utf-8")
    ctx = ServiceContext(store=store, token_store=TokenStore(token_path), query=QueryKernel(store))
    return TestClient(create_app(ctx)), store, tmp


def _auth():
    return {"Authorization": "Bearer tok-brief"}


def test_brief_generate_requires_write_scope():
    client, _, tmp = _make_client(["read"])  # no write scope
    try:
        resp = client.post("/v16/brief/generate",
                           json={"items": [], "as_of": None}, headers=_auth())
        assert resp.status_code in (401, 403)
    finally:
        tmp.cleanup()


def test_brief_generate_publishes_brief_and_cards():
    client, store, tmp = _make_client(["write"])
    try:
        payload = {
            "items": [
                {"kind": "model_probe", "source_id": "Nemotron-3-Ultra",
                 "title": "探针可达", "body": "延迟 42ms", "url": "https://x/v1/models",
                 "status": "ok"},
                {"kind": "github_release", "source_id": "litellm/litellm",
                 "title": "v1.50.0 发布", "body": "修复路由超时",
                 "url": "https://github.com/BerriAI/litellm/releases/tag/v1.50.0"},
            ],
            "as_of": "2026-10-26T07:30:00Z",
        }
        resp = client.post("/v16/brief/generate", json=payload, headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["brief_fact_id"]
        assert "Nemotron-3-Ultra" in body["brief_markdown"]
        assert len(body["cards"]) == 2
        names = {c["name"] for c in body["cards"]}
        assert "Nemotron-3-Ultra" in names
        assert "litellm/litellm" in names
        # Entities really landed in the store.
        with store.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        assert n == 2
    finally:
        tmp.cleanup()
