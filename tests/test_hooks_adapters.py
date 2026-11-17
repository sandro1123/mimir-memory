# -*- coding: utf-8 -*-
"""A-tier hook adapter scaffolding (1.8.0): declarative payload builders."""
from __future__ import annotations

import pytest

from mimir_v8.adapters.hooks_a_tier import HOOKS_A_TIER, build_session_end_payload
from mimir_v8.schema import ValidationError


def test_hooks_a_tier_declares_start_end_paths():
    assert set(HOOKS_A_TIER) == {"claude-code", "codex"}
    for spec in HOOKS_A_TIER.values():
        assert spec["start"] == "/v8/hooks/session-start"
        assert spec["end"] == "/v8/hooks/session-end"


def test_build_session_end_payload_is_valid_envelope():
    payload = build_session_end_payload(
        connector_type="claude-code",
        connector_id="claude-code",
        session_id="sess-1",
        owner_principal="jarvis",
        messages=[{"role": "user", "content": "hi"}],
        idempotency_key="hook:sess-1:end",
    )
    assert payload["memory_mode"] == "observe"
    assert payload["owner_principal"] == "jarvis"
    assert payload["connector_type"] == "claude-code"
    assert payload["session_id"] == "sess-1"
    assert payload["idempotency_key"] == "hook:sess-1:end"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_build_session_end_payload_rejects_explicit():
    with pytest.raises(ValidationError):
        build_session_end_payload(
            connector_type="claude-code",
            connector_id="claude-code",
            session_id="sess-1",
            owner_principal="jarvis",
            messages=[{"role": "user", "content": "hi"}],
            idempotency_key="hook:sess-1:end",
            memory_mode="explicit",
        )
