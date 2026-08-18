"""Mímir v9.2 Hermes MemoryProvider plugin.

Implements the Hermes MemoryProvider interface for native memory integration.
Provides search/save/inject lifecycle hooks and compression rescue.

Install: cp to ~/.hermes/plugins/ and configure memory.provider=mimir
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

MIMIR_API = os.environ.get("MIMIR_PLUGIN_API", "http://127.0.0.1:8456")
ADMIN_TOKEN_FILE = Path(os.environ.get("MIMIR_PLUGIN_TOKEN_FILE", str(Path.home() / ".hermes/mimir/secrets/clients/admin.token")))


def _get_token() -> str | None:
    if ADMIN_TOKEN_FILE.exists():
        return ADMIN_TOKEN_FILE.read_text().strip()
    return None


def _mimir_post(path: str, data: dict) -> dict | None:
    token = _get_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            f"{MIMIR_API}{path}",
            data=json.dumps(data).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _mimir_get(path: str) -> dict | None:
    token = _get_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(f"{MIMIR_API}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


# ── Hermes MemoryProvider Interface ──────────────────────


def search(query: str, limit: int = 5, **kwargs) -> list[dict]:
    """Search Mímir for relevant memories."""
    result = _mimir_post("/v8/query", {"text": query, "limit": limit})
    if result:
        return result.get("results", [])
    return []


def save(content: str, owner: str = "mentor", domain: str = "personal", fact_type: str = "user_pref", **kwargs) -> dict | None:
    """Save a memory via remember endpoint."""
    return _mimir_post("/v8/learning/remember", {
        "content": content,
        "owner_principal": owner,
        "domain": domain,
        "fact_type": fact_type,
        "idempotency_key": f"plugin-remember:{hash(content)}",
    })


def inject() -> list[dict]:
    """Get core memories for injection into agent context."""
    # Use the mentor core memory injection endpoint
    return _mimir_get("/v8/core-memory/mentor/inject")


def ingest_conversation(messages: list[dict], owner: str = "mentor", **kwargs) -> dict | None:
    """Ingest a conversation turn for memory extraction."""
    return _mimir_post("/v8/ingestion/conversations", {
        "connector_type": "hermes_cdc",
        "connector_id": "hermes-plugin",
        "owner_principal": owner,
        "memory_mode": "observe",
        "retention_class": "standard",
        "messages": messages,
        "idempotency_key": f"plugin-ingest:{hash(str(messages))}",
    })


def health() -> dict:
    """Check if Mímir API is reachable."""
    result = _mimir_get("/health")
    if result:
        return {"status": "ok", "version": result.get("version", "?")}
    return {"status": "error", "version": "unreachable"}


# ── Compression Rescue Hook ──────────────────────────────

def on_compression(session_id: str, messages: list[dict], **kwargs) -> None:
    """Called before Hermes compacts a session. Rescues conversation content."""
    ingest_conversation(messages, kwargs.get("owner", "mentor"))


# ── Plugin Metadata ──────────────────────────────────────

PLUGIN_NAME = "mimir-memory-provider"
PLUGIN_VERSION = "9.2.0"
PLUGIN_DESCRIPTION = "Mímir federated memory system — search, save, ingest, and compression rescue"

__all__ = ["search", "save", "inject", "ingest_conversation", "health", "on_compression", "PLUGIN_NAME", "PLUGIN_VERSION"]
