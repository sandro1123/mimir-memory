# -*- coding: utf-8 -*-
"""#34 hooks — end-to-end hook pipeline integration (1.8.0 PR1)."""
from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.learning import LearningService
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


def _app(tmp_path):
    store = CanonicalStore(tmp_path / "canonical.db")
    token_path = tmp_path / "tokens.json"
    token_path.write_text(json.dumps({
        "principals": [
            {
                "id": "jarvis",
                "token_sha256": hashlib.sha256(b"tok-jarvis").hexdigest(),
                "scopes": ["read", "ingest"],
                "roles": [],
                "admin": False,
            }
        ]
    }), encoding="utf-8")
    ctx = ServiceContext(
        store=store,
        token_store=TokenStore(token_path),
        query=QueryKernel(store),
        learning=LearningService(store),
    )
    return store, TestClient(create_app(ctx), raise_server_exceptions=False)


def test_hook_pipeline_start_then_end_crystallizes_candidate(tmp_path):
    store, client = _app(tmp_path)
    # SessionStart: agent gets identity-scoped context
    start = client.get("/v8/hooks/session-start", headers={"Authorization": "Bearer tok-jarvis"})
    assert start.status_code == 200, start.text
    assert start.json()["principal_id"] == "jarvis"

    # SessionEnd: transcript crystallizes into one STM candidate (never fact)
    end = client.post(
        "/v8/hooks/session-end",
        json={
            "connector_type": "hook",
            "connector_id": "session-end",
            "session_id": "sess-int-1",
            "owner_principal": "jarvis",
            "memory_mode": "observe",
            "messages": [
                {"role": "user", "content": "记住：1.8 hooks pipeline 集成完成"},
                {"role": "assistant", "content": "好的，已记录待审核候选。"},
            ],
            "idempotency_key": "hook-int-1",
        },
        headers={"Authorization": "Bearer tok-jarvis"},
    )
    assert end.status_code in (200, 201), end.text
    body = end.json()
    assert body["candidate_id"]
    assert body["idempotent_replay"] is False

    # candidate is queryable by owner and remains a candidate (no direct fact write)
    with store.connect() as c:
        cand = c.execute(
            "SELECT candidate_id, proposed_owner_principal, status FROM candidate_facts WHERE candidate_id=?",
            (body["candidate_id"],),
        ).fetchone()
        assert cand is not None
        assert cand["proposed_owner_principal"] == "jarvis"
        assert cand["status"] == "review_required"
        fact_count = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert fact_count == 0


def test_hook_pipeline_end_is_idempotent(tmp_path):
    _store, client = _app(tmp_path)
    payload = {
        "connector_type": "hook",
        "connector_id": "session-end",
        "session_id": "sess-int-2",
        "owner_principal": "jarvis",
        "memory_mode": "observe",
        "messages": [{"role": "user", "content": "same session replay"}],
        "idempotency_key": "hook-int-replay",
    }
    headers = {"Authorization": "Bearer tok-jarvis"}
    a = client.post("/v8/hooks/session-end", json=payload, headers=headers)
    b = client.post("/v8/hooks/session-end", json=payload, headers=headers)
    assert a.status_code in (200, 201) and b.status_code in (200, 201)
    assert a.json()["candidate_id"] == b.json()["candidate_id"]


def test_hook_pipeline_session_start_hides_other_principal_data(tmp_path):
    store, client = _app(tmp_path)
    store.create_fact(CreateFact(
        content="mentor-secret", summary="s", owner_principal="mentor", domain="personal",
        fact_type="user_pref", visibility="owner_only", sensitivity="internal",
        egress_policy="local_only", human_status="confirmed",
    ), actor_principal="mentor")
    resp = client.get("/v8/hooks/session-start", headers={"Authorization": "Bearer tok-jarvis"})
    assert resp.status_code == 200
    texts = [f.get("content") or f.get("summary") for f in resp.json()["user_prefs"] + resp.json()["iron_rules"]]
    assert "mentor-secret" not in texts
