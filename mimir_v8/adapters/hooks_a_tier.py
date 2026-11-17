"""Declarative A-tier hook adapter scaffolding (Claude Code / Codex).

Keeps backend contract stable by targeting existing /v8/hooks/* endpoints and
building ConversationEnvelope-compatible payloads. No direct fact writes.
"""
from __future__ import annotations

from mimir_v8.schema import ValidationError

HOOKS_A_TIER: dict[str, dict[str, str]] = {
    "claude-code": {"start": "/v8/hooks/session-start", "end": "/v8/hooks/session-end"},
    "codex": {"start": "/v8/hooks/session-start", "end": "/v8/hooks/session-end"},
}


def build_session_end_payload(
    *,
    connector_type: str,
    connector_id: str,
    session_id: str,
    owner_principal: str,
    messages: list[dict],
    idempotency_key: str,
    memory_mode: str = "observe",
    title: str | None = None,
) -> dict:
    """Build a ConversationEnvelope-compatible payload for /v8/hooks/session-end.

    Explicit memory writes must go through remember(), not hooks (spec §5.1).
    """
    if memory_mode == "explicit":
        raise ValidationError("explicit memory_mode must use remember(), not the session hook")
    return {
        "connector_type": connector_type,
        "connector_id": connector_id,
        "session_id": session_id,
        "owner_principal": owner_principal,
        "memory_mode": memory_mode,
        "retention_class": "short",
        "messages": messages,
        "idempotency_key": idempotency_key,
        **({"title": title} if title is not None else {}),
    }
