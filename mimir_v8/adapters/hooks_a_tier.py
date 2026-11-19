"""Declarative A-tier hook adapter scaffolding (Claude Code / Codex).

1.8.0: the builtin pair is registered in ``mimir_v8.adapters.registry``; this
module remains the compatibility facade — ``HOOKS_A_TIER`` and
``build_session_end_payload`` keep their pre-registry behavior so existing
imports/tests are untouched. New code should consume the registry.

Backend contract stays stable: payloads target the existing /v8/hooks/*
endpoints. No direct fact writes; ``memory_mode="explicit"`` is rejected —
explicit memory belongs to ``remember()``.
"""
from __future__ import annotations

from mimir_v8.adapters.registry import get_registry
from mimir_v8.schema import ValidationError

#: Compatibility view: adapter_id -> hook endpoint paths (pre-1.8 shape).
HOOKS_A_TIER: dict[str, dict[str, str]] = {
    adapter_id: {
        "start": spec.hooks_start.path if spec.hooks_start else None,
        "end": spec.hooks_end.path if spec.hooks_end else None,
    }
    for adapter_id in ("claude-code", "codex")
    if (spec := get_registry().get(adapter_id)) is not None
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
    adapter_id: str | None = None,
) -> dict:
    """Build a ConversationEnvelope-compatible payload for /v8/hooks/session-end.

    With ``adapter_id`` the registry's per-adapter defaults (memory mode,
    retention class) apply where the caller has not pinned an override;
    without it the pre-1.8 defaults are used, byte-for-byte.
    Explicit memory writes must go through remember(), not hooks (spec §5.1).
    """
    if memory_mode == "explicit":
        raise ValidationError("explicit memory_mode must use remember(), not the session hook")
    retention_class = "short"
    if adapter_id is not None:
        spec = get_registry().get(adapter_id)
        if spec is not None:
            retention_class = spec.default_retention_class
    return {
        "connector_type": connector_type,
        "connector_id": connector_id,
        "session_id": session_id,
        "owner_principal": owner_principal,
        "memory_mode": memory_mode,
        "retention_class": retention_class,
        "messages": messages,
        "idempotency_key": idempotency_key,
        **({"title": title} if title is not None else {}),
    }
