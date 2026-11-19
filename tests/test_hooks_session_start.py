# -*- coding: utf-8 -*-
"""#34 hooks — SessionStart returns a scoped bootstrap payload (1.8.0 A-tier)."""
from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.query import QueryKernel
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _token_doc(token_plain, principal_id, scopes):
    return json.dumps({
        "principals": [{
            "id": principal_id,
            "token_sha256": hashlib.sha256(token_plain.encode("utf-8")).hexdigest(),
            "scopes": scopes,
            "roles": [],
            "admin": False,
        }]
    })


def _ctx(tmp_path):
    store = CanonicalStore(tmp_path / "canonical.db")
    token_path = tmp_path / "tokens.json"
    token_path.write_text(_token_doc("tok-jarvis", "jarvis", ["read"]), encoding="utf-8")
    return store, token_path


def test_session_start_returns_scoped_bootstrap(tmp_path):
    store, token_path = _ctx(tmp_path)
    client = TestClient(
        create_app(ServiceContext(store=store, token_store=TokenStore(token_path), query=QueryKernel(store))),
        raise_server_exceptions=False,
    )
    resp = client.get("/v8/hooks/session-start", headers={"Authorization": "Bearer tok-jarvis"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # same shape as /v15/bootstrap (identity-scoped)
    assert body["principal_id"] == "jarvis"
    assert "iron_rules" in body and "user_prefs" in body and "domain_counts" in body
    assert isinstance(body["iron_rules"], list)


def test_session_start_respects_identity_visibility(tmp_path):
    store, token_path = _ctx(tmp_path)
    # private fact for another principal must not leak into jarvis bootstrap
    store.create_fact(CreateFact(
        content="secret-other", summary="s", owner_principal="mentor", domain="personal",
        fact_type="user_pref", visibility="owner_only", sensitivity="internal",
        egress_policy="local_only", human_status="confirmed",
    ), actor_principal="mentor")
    # own fact for the caller must be visible (guards can_read arg-order regressions)
    own = store.create_fact(CreateFact(
        content="own-visible-pref", summary="own", owner_principal="jarvis", domain="personal",
        fact_type="user_pref", visibility="owner_only", sensitivity="internal",
        egress_policy="local_only", human_status="confirmed",
    ), actor_principal="jarvis")
    client = TestClient(
        create_app(ServiceContext(store=store, token_store=TokenStore(token_path), query=QueryKernel(store))),
        raise_server_exceptions=False,
    )
    resp = client.get("/v8/hooks/session-start", headers={"Authorization": "Bearer tok-jarvis"})
    assert resp.status_code == 200
    body = resp.json()
    texts = [f.get("content") or f.get("summary") for f in body["user_prefs"] + body["iron_rules"]]
    fact_ids = [f.get("fact_id") for f in body["user_prefs"] + body["iron_rules"]]
    assert "secret-other" not in texts
    assert own["fact_id"] in fact_ids and "own-visible-pref" in texts


def test_session_start_requires_read_scope(tmp_path):
    store = CanonicalStore(tmp_path / "canonical.db")
    token_path = tmp_path / "tokens.json"
    token_path.write_text(_token_doc("tok-ingest", "jarvis", ["ingest"]), encoding="utf-8")
    client = TestClient(
        create_app(ServiceContext(store=store, token_store=TokenStore(token_path), query=QueryKernel(store))),
        raise_server_exceptions=False,
    )
    resp = client.get("/v8/hooks/session-start", headers={"Authorization": "Bearer tok-ingest"})
    assert resp.status_code in (401, 403)
