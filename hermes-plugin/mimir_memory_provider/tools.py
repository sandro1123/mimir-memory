"""Mímir v12 MemoryProvider tools — mimir_search / remember / recent / reflect.

Thin HTTP clients over the Mímir API. Fail closed: on network or auth error they
return an empty result rather than raising into the agent loop.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MIMIR_API = os.environ.get("MIMIR_PLUGIN_API", "http://127.0.0.1:8456")
ADMIN_TOKEN_FILE = Path(os.environ.get(
    "MIMIR_PLUGIN_TOKEN_FILE", str(Path.home() / ".hermes/mimir/secrets/clients/admin.token")
))


def _token() -> str | None:
    if ADMIN_TOKEN_FILE.exists():
        return ADMIN_TOKEN_FILE.read_text().strip()
    return None


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _post(path: str, data: dict) -> dict | None:
    try:
        request = urllib.request.Request(
            f"{MIMIR_API}{path}", data=json.dumps(data).encode(),
            headers=_headers(), method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def _get(path: str) -> dict | None:
    try:
        request = urllib.request.Request(f"{MIMIR_API}{path}", headers=_headers())
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def mimir_search(query: str, limit: int = 5, **kwargs) -> list[dict]:
    """Search Mímir memory for facts relevant to a query."""
    result = _post("/v8/query", {"text": query, "limit": limit})
    if result:
        return result.get("results", [])
    return []


def mimir_remember(content: str, owner: str = "mentor", domain: str = "personal",
                   fact_type: str = "user_pref", **kwargs) -> dict | None:
    """Persist a memory fact into Mímir."""
    return _post("/v8/learning/remember", {
        "content": content,
        "owner_principal": owner,
        "domain": domain,
        "fact_type": fact_type,
        "idempotency_key": f"plugin-remember:{hash(content)}",
    })


def mimir_recent(limit: int = 10, **kwargs) -> list[dict]:
    """Return the most recently recorded memories."""
    result = _get(f"/v8/memories/recent?limit={int(limit)}")
    if result:
        return result.get("results", result.get("memories", []))
    return []


def mimir_reflect(topic: str = "", **kwargs) -> dict | None:
    """Reflect on stored knowledge for a topic (quality dashboard)."""
    if topic:
        result = _post("/v8/query", {"query": topic, "limit": 10})
        return {"topic": topic, "results": result.get("results", []) if result else []}
    return _get("/v12/evolve/report")


__all__ = [
    "mimir_search", "mimir_remember", "mimir_recent", "mimir_reflect",
    "_post", "_get",
]