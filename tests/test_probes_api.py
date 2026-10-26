# -*- coding: utf-8 -*-
"""API tests for v1.6.0 Step 2 probe & source registry endpoints."""
import sys
import tempfile
import json
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from mimir_v8.api import create_app, ServiceContext
from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact
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
            "token_sha256": hashlib.sha256("tok-probe".encode()).hexdigest(),
            "scopes": scopes,
            "roles": ["authenticated"],
            "admin": admin,
        }]
    }), encoding="utf-8")
    ctx = ServiceContext(store=store, token_store=TokenStore(token_path), query=QueryKernel(store))
    return TestClient(create_app(ctx)), store, tmp


def _auth():
    return {"Authorization": "Bearer tok-probe"}


def test_probe_status_requires_read_scope():
    client, _, tmp = _make_client(["write"])  # no read scope
    try:
        resp = client.get("/v16/probe/status", headers=_auth())
        assert resp.status_code in (401, 403)
    finally:
        tmp.cleanup()


def test_probe_status_returns_summary():
    client, store, tmp = _make_client(["read"])
    try:
        # Seed a model_probe entity so the endpoint has something to surface.
        fact = store.create_fact(CreateFact(
            content="Nemotron probe fact.", summary="np",
            domain="system", fact_type="project_config",
            owner_principal="heimdallr",
        ), actor_principal="heimdallr")
        store.record_model_probe(
            model_name="Nemotron-3-Ultra", domain="free_model_pool",
            outcome={"status": "ok", "latency_ms": 10.0, "error": None,
                      "endpoint": "https://api.example.com/v1/models", "expected_status": [200]},
            fact_id=fact["fact_id"], actor_principal="heimdallr",
        )

        resp = client.get("/v16/probe/status", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert "probes" in body
        assert body["models_total"] >= 1
        names = [p["name"] for p in body["probes"]]
        assert "Nemotron-3-Ultra" in names
        probe = next(p for p in body["probes"] if p["name"] == "Nemotron-3-Ultra")
        assert probe["last_status"] == "active"
    finally:
        tmp.cleanup()


def test_probe_replay_requires_write_scope():
    client, _, tmp = _make_client(["read"])  # no write scope
    try:
        resp = client.post("/v16/probe/replay", json={"now": None}, headers=_auth())
        assert resp.status_code in (401, 403)
    finally:
        tmp.cleanup()


def test_probe_replay_completes_due_tasks():
    client, store, tmp = _make_client(["write"])
    try:
        # Queue a due probe task; the endpoint will re-probe the (fake) endpoint
        # and mark it completed. We assert the queue drains without error.
        store.queue_probe_task(
            entity_id="ent_x",
            endpoint="http://unreachable.invalid.local/v1/models",
            snapshot={"status": "unreachable", "error": "timed out"},
            due_at="2026-01-01T00:00:00Z",
        )
        resp = client.post("/v16/probe/replay", json={}, headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["replayed"] == 1
        # Queue is now empty for that window.
        assert store.get_due_probe_tasks(now="2099-01-01T00:00:00Z") == []
    finally:
        tmp.cleanup()
