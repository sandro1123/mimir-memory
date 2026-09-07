"""Mímir v14 MemoryProvider tools — mimir_search / remember / recent / reflect / feedback.

Thin HTTP clients over the Mímir API. Fail closed: on network or auth error they
return an empty result (reads) or an explicit ``{"ok": False, "error": ...}``
(writes) rather than raising into the agent loop.

2026-09-07 audit fixes:
- reads used to authenticate as ``admin`` for every agent, so owner_only facts
  leaked across agents in prefetch; ``_token()`` now prefers ``<owner>.token``.
- ``mimir_remember`` used to return ``{}`` on failure (agent believed it was
  stored) and keyed idempotency on Python ``hash()`` (randomised per process).
- ``mimir_reflect`` posted ``{"query": ...}`` while the API requires ``text``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("mimir_memory_provider")

MIMIR_API = os.environ.get("MIMIR_PLUGIN_API", "http://127.0.0.1:8456")
TOKEN_DIR_DEFAULT = Path.home() / ".hermes/mimir/secrets/clients"
ADMIN_TOKEN_FILE = Path(os.environ.get(
    "MIMIR_PLUGIN_TOKEN_FILE", str(TOKEN_DIR_DEFAULT / "admin.token")
))

# Mutable per-process state populated by the provider's initialize() hook.
_STATE: dict[str, Any] = {}


def _owner() -> str:
    """Resolve the calling agent's principal for ACL scoping.

    Priority: MIMIR_PLUGIN_OWNER env (set per gateway systemd unit) >
    agent_identity passed by Hermes at provider init > legacy default.
    """
    env = os.environ.get("MIMIR_PLUGIN_OWNER", "").strip()
    if env:
        return env
    ident = str(_STATE.get("agent_identity") or "").strip()
    if ident and ident not in ("default", "custom"):
        return ident
    return "mentor"


def _token() -> str | None:
    """Bearer token for the calling agent.

    Priority: explicit MIMIR_PLUGIN_TOKEN_FILE > <token dir>/<owner>.token >
    <token dir>/admin.token. Using the agent's own token makes Mímir's ACL
    apply to reads (prefetch/search), not just to writes.
    """
    explicit = os.environ.get("MIMIR_PLUGIN_TOKEN_FILE", "").strip()
    if explicit:
        path = Path(explicit)
        if path.exists():
            return path.read_text().strip()
    token_dir = Path(os.environ.get("MIMIR_PLUGIN_TOKEN_DIR", str(TOKEN_DIR_DEFAULT)))
    agent_file = token_dir / f"{_owner()}.token"
    if agent_file.exists():
        return agent_file.read_text().strip()
    admin = token_dir / "admin.token"
    if admin.exists():
        return admin.read_text().strip()
    if ADMIN_TOKEN_FILE.exists():
        return ADMIN_TOKEN_FILE.read_text().strip()
    return None


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _error(kind: str, **detail) -> dict:
    logger.warning("mimir plugin %s: %s", kind, detail)
    return {"error": {"kind": kind, **detail}}


def _ok(result) -> bool:
    return isinstance(result, dict) and "error" not in result


def _post(path: str, data: dict) -> dict | None:
    try:
        request = urllib.request.Request(
            f"{MIMIR_API}{path}", data=json.dumps(data).encode(),
            headers=_headers(), method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode(errors="replace")[:300]
        except Exception:  # noqa: BLE001 - body is best-effort diagnostics
            pass
        return _error("http", status=exc.code, path=path, body=body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _error("unreachable", path=path, detail=str(exc)[:200])


def _get(path: str) -> dict | None:
    try:
        request = urllib.request.Request(f"{MIMIR_API}{path}", headers=_headers())
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return _error("http", status=exc.code, path=path)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _error("unreachable", path=path, detail=str(exc)[:200])


def _idempotency_key(owner: str, content: str) -> str:
    """Stable across processes (Python ``hash()`` is salted per interpreter)."""
    digest = hashlib.sha256(f"{owner}\n{content}".encode("utf-8")).hexdigest()[:32]
    return f"plugin-remember:{digest}"


def mimir_search(query: str, limit: int = 5, **kwargs) -> list[dict]:
    """Search Mímir memory for facts relevant to a query (ACL-scoped to the agent)."""
    result = _post("/v8/query", {"text": query, "limit": limit})
    return result.get("results", []) if _ok(result) else []


def mimir_remember(content: str, owner: str = "", domain: str = "personal",
                   fact_type: str = "user_pref", **kwargs) -> dict:
    """Persist a memory fact into Mímir (owner defaults to the active agent).

    Returns ``{"ok": True, ...api response}`` or ``{"ok": False, "error": str}``
    so the agent never mistakes a failed write for a stored memory.
    """
    principal = owner or _owner()
    result = _post("/v8/learning/remember", {
        "content": content,
        "owner_principal": principal,
        "domain": domain,
        "fact_type": fact_type,
        "idempotency_key": _idempotency_key(principal, content),
    })
    if _ok(result):
        return {"ok": True, **result}
    err = (result or {}).get("error", {}) if isinstance(result, dict) else {}
    parts = [str(err.get("kind", "unknown")), str(err.get("status", "")),
             str(err.get("detail", err.get("body", "")))]
    return {"ok": False, "error": " ".join(p for p in parts if p).strip()}


def mimir_recent(limit: int = 10, **kwargs) -> list[dict]:
    """Return the most recently recorded memories."""
    result = _get(f"/v8/memories/recent?limit={int(limit)}")
    if _ok(result):
        return result.get("results", result.get("memories", []))
    return []


def mimir_reflect(topic: str = "", **kwargs) -> dict | None:
    """Reflect on stored knowledge for a topic (quality dashboard)."""
    if topic:
        result = _post("/v8/query", {"text": topic, "limit": 10})
        return {"topic": topic, "results": result.get("results", []) if _ok(result) else []}
    result = _get("/v12/evolve/report")
    return result if _ok(result) else None


def mimir_feedback(query: str, fact_id: str, signal: str, **kwargs) -> dict | None:
    """Submit a retrieval-quality signal so Mímir retrieval can self-evolve.

    signal: "useful" (result answered the query), "useless" (irrelevant/wrong),
    or "correction" (fact is outdated/incorrect). Feeds the EvolveMem loop
    (aggregated nightly; facts need >= 2 signals to adjust confidence).
    """
    signal = signal.strip().lower()
    if signal not in ("useful", "useless", "correction"):
        return {"error": f"invalid signal {signal!r}; use useful|useless|correction"}
    return _post("/v12/evolve/feedback", {
        "query_text": query,
        "fact_id": fact_id,
        "signal": signal,
    })


__all__ = [
    "mimir_search", "mimir_remember", "mimir_recent", "mimir_reflect",
    "mimir_feedback", "_post", "_get",
]
