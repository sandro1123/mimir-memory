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

2026-11-27 identity fix (Jarvis isolation):
- The identity used to live in a module-global ``_STATE`` that nothing ever
  filled, so every profile resolved to the ``mentor`` fallback; a missing
  ``<owner>.token`` then fell back to ``admin.token``. The gateway multiplexes
  several profiles in ONE process, so any process-wide identity (module state
  or env var) is wrong by construction.
- Now every call carries an explicit ``owner`` resolved per provider instance
  from the Hermes profile. Unknown profile, missing token -> fail closed
  (no request is sent). There is no mentor or admin fallback any more.
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

#: Hermes profile name -> Mímir principal. Explicit allow-list: a profile that
#: is not listed gets NO identity and the plugin refuses to talk to Mímir.
#: ``default`` is the main Hermes profile, which has always written as mentor.
PROFILE_PRINCIPALS: dict[str, str] = {
    "default": "mentor",
    "jarvis": "jarvis",
    "quantmaster": "quantmaster",
}

#: Principals the plugin must never authenticate as, whatever the mapping says.
FORBIDDEN_PRINCIPALS = frozenset({"admin"})


def token_dir() -> Path:
    return Path(os.environ.get("MIMIR_PLUGIN_TOKEN_DIR", str(TOKEN_DIR_DEFAULT)))


def resolve_owner(agent_identity: str = "") -> str | None:
    """Map a Hermes profile name to a Mímir principal, or ``None`` (fail closed).

    ``MIMIR_PLUGIN_OWNER`` is honoured only when Hermes passed no profile at
    all (standalone / CLI use). It is process-wide, so it must never override
    a real profile identity inside the multiplexing gateway.
    """
    ident = str(agent_identity or "").strip()
    if ident:
        owner = PROFILE_PRINCIPALS.get(ident)
    else:
        owner = os.environ.get("MIMIR_PLUGIN_OWNER", "").strip() or None
    if not owner or owner in FORBIDDEN_PRINCIPALS:
        return None
    return owner


def _token(owner: str | None) -> str | None:
    """Bearer token for exactly this principal: ``<token dir>/<owner>.token``.

    No fallback to any other principal's token. A missing file means no
    request is sent at all.
    """
    if not owner or owner in FORBIDDEN_PRINCIPALS:
        return None
    path = token_dir() / f"{owner}.token"
    if not path.is_file():
        return None
    token = path.read_text().strip()
    return token or None


def _error(kind: str, **detail) -> dict:
    logger.warning("mimir plugin %s: %s", kind, detail)
    return {"error": {"kind": kind, **detail}}


def _ok(result) -> bool:
    return isinstance(result, dict) and "error" not in result


def _headers(owner: str | None) -> dict[str, str] | None:
    token = _token(owner)
    if not token:
        return None
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _post(path: str, data: dict, *, owner: str | None) -> dict:
    headers = _headers(owner)
    if headers is None:
        return _error("no_identity", path=path, owner=owner)
    try:
        request = urllib.request.Request(
            f"{MIMIR_API}{path}", data=json.dumps(data).encode(),
            headers=headers, method="POST",
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


def _get(path: str, *, owner: str | None) -> dict:
    headers = _headers(owner)
    if headers is None:
        return _error("no_identity", path=path, owner=owner)
    try:
        request = urllib.request.Request(f"{MIMIR_API}{path}", headers=headers)
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


def mimir_search(query: str, limit: int = 5, *, owner: str | None = None, **kwargs) -> list[dict]:
    """Search Mímir memory for facts relevant to a query (ACL-scoped to ``owner``)."""
    # depth=deep: 插件是 agent 的主召回入口, 须能命中 L1 原子事实
    # (event/project_config/reference/...)。standard 档的 L1 gate 会把这些
    # 整条丢弃, 而它们占生产库 67%(485/721)。spec 不动, 由调用方表达意图。
    result = _post("/v8/query", {"text": query, "limit": limit, "depth": "deep"}, owner=owner)
    return result.get("results", []) if _ok(result) else []


def mimir_remember(content: str, owner: str | None = None, domain: str = "personal",
                   fact_type: str = "user_pref", **kwargs) -> dict:
    """Persist a memory fact into Mímir as ``owner``.

    Returns ``{"ok": True, ...api response}`` or ``{"ok": False, "error": str}``
    so the agent never mistakes a failed write for a stored memory.
    """
    if not owner:
        return {"ok": False, "error": "no_identity"}
    result = _post("/v8/learning/remember", {
        "content": content,
        "owner_principal": owner,
        "domain": domain,
        "fact_type": fact_type,
        "idempotency_key": _idempotency_key(owner, content),
    }, owner=owner)
    if _ok(result):
        return {"ok": True, **result}
    err = (result or {}).get("error", {}) if isinstance(result, dict) else {}
    parts = [str(err.get("kind", "unknown")), str(err.get("status", "")),
             str(err.get("detail", err.get("body", "")))]
    return {"ok": False, "error": " ".join(p for p in parts if p).strip()}


def mimir_recent(limit: int = 10, *, owner: str | None = None, **kwargs) -> list[dict]:
    """Return the most recently recorded memories visible to ``owner``."""
    result = _get(f"/v8/memories/recent?limit={int(limit)}", owner=owner)
    if _ok(result):
        return result.get("results", result.get("memories", []))
    return []


def mimir_reflect(topic: str = "", *, owner: str | None = None, **kwargs) -> dict | None:
    """Reflect on stored knowledge for a topic (quality dashboard)."""
    if topic:
        result = _post("/v8/query", {"text": topic, "limit": 10, "depth": "deep"}, owner=owner)
        return {"topic": topic, "results": result.get("results", []) if _ok(result) else []}
    result = _get("/v12/evolve/report", owner=owner)
    return result if _ok(result) else None


def mimir_feedback(query: str, fact_id: str, signal: str, *, owner: str | None = None,
                   **kwargs) -> dict | None:
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
    }, owner=owner)


__all__ = [
    "mimir_search", "mimir_remember", "mimir_recent", "mimir_reflect",
    "mimir_feedback", "resolve_owner", "PROFILE_PRINCIPALS", "_post", "_get",
]
