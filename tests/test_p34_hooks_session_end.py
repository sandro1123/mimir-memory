# -*- coding: utf-8 -*-
"""#34 hooks — SessionEnd auto-crystallizes a candidate (1.8.0 §5.2 A-tier).

Covers: ingest + one candidate, idempotent replay (same candidate_id,
no duplicate), memory_mode gate (explicit rejected), DLP redaction,
and no direct fact writes.
"""
from __future__ import annotations

import pytest

from mimir_v8.learning import (
    ConversationEnvelope,
    ConversationMessage,
    LearningService,
)
from mimir_v8.schema import ValidationError
from mimir_v8.store import CanonicalStore


def _store(tmp_path):
    return CanonicalStore(tmp_path / "canonical.db")


def _envelope(**over):
    base = dict(
        connector_type="external_agent",
        connector_id="claude-code",
        session_id="sess_1",
        owner_principal="jarvis",
        memory_mode="observe",
        retention_class="short",
        messages=(
            ConversationMessage(role="user", content="我记得了：Q3 季度目标定在 5%"),
            ConversationMessage(role="assistant", content="好的，已记录。"),
        ),
        idempotency_key="hook-sess_1",
    )
    base.update(over)
    return ConversationEnvelope(**base)


def test_session_end_ingests_and_crystallizes(tmp_path):
    svc = LearningService(_store(tmp_path))
    result = svc.session_end(_envelope(), actor_principal="jarvis")
    assert result["status"] == "stored"
    assert result["candidate_id"]
    assert result["source_id"]
    # Candidate row exists and references the conversation source
    with svc.store.connect() as c:
        cand = c.execute(
            "SELECT * FROM candidate_facts WHERE candidate_id=?",
            (result["candidate_id"],),
        ).fetchone()
        assert cand is not None
        assert cand["source_id"] == result["source_id"]
        assert "auto_captured_requires_review" in (cand["uncertainty_json"] or "")
        # No facts were written
        assert c.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        # Conversation source exists (STM layer)
        assert c.execute(
            "SELECT COUNT(*) FROM conversation_sources WHERE source_id=?",
            (result["source_id"],),
        ).fetchone()[0] == 1


def test_session_end_idempotent_replay_same_candidate(tmp_path):
    svc = LearningService(_store(tmp_path))
    first = svc.session_end(_envelope(), actor_principal="jarvis")
    second = svc.session_end(_envelope(), actor_principal="jarvis")
    assert first["candidate_id"] == second["candidate_id"]
    assert second["idempotent_replay"] is True
    with svc.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0] == 1


def test_session_end_rejects_explicit_memory_mode(tmp_path):
    svc = LearningService(_store(tmp_path))
    with pytest.raises(ValidationError):
        svc.session_end(_envelope(memory_mode="explicit"), actor_principal="jarvis")


def test_session_end_redacts_secrets(tmp_path):
    svc = LearningService(_store(tmp_path))
    result = svc.session_end(
        _envelope(messages=(ConversationMessage(role="user", content="我的密钥是 sk-4166fcd0123456789abcdef"),)),
        actor_principal="jarvis",
    )
    assert result["candidate_id"]
    with svc.store.connect() as c:
        cand = c.execute(
            "SELECT content FROM candidate_facts WHERE candidate_id=?",
            (result["candidate_id"],),
        ).fetchone()
        assert "sk-4166" not in cand["content"]
        assert "[REDACTED]" in cand["content"]


def test_api_session_end_wires(tmp_path):
    """HTTP layer: POST /v8/hooks/session-end → candidate created."""
    import hashlib
    import json

    from fastapi.testclient import TestClient

    from mimir_v8.api import ServiceContext, create_app
    from mimir_v8.auth import TokenStore

    store = _store(tmp_path)
    token_path = tmp_path / "tokens.json"
    token_path.write_text(
        json.dumps({
            "principals": [
                {
                    "id": "jarvis",
                    "token_sha256": hashlib.sha256(b"tok-jarvis").hexdigest(),
                    "scopes": ["ingest"],
                    "roles": [],
                    "admin": False,
                },
            ]
        }),
        encoding="utf-8",
    )
    from mimir_v8.query import QueryKernel

    client = TestClient(
        create_app(
            ServiceContext(
                store=store,
                token_store=TokenStore(token_path),
                query=QueryKernel(store),
                learning=LearningService(store),
            )
        ),
        raise_server_exceptions=False,
    )
    resp = client.post(
        "/v8/hooks/session-end",
        json={
            "connector_type": "external_agent",
            "connector_id": "claude-code",
            "session_id": "sess_api",
            "owner_principal": "jarvis",
            "memory_mode": "observe",
            "retention_class": "short",
            "messages": [{"role": "user", "content": "记一下：周末部署完成"}],
            "idempotency_key": "hook-api-sess",
        },
        headers={"Authorization": "Bearer tok-jarvis"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["candidate_id"]
