"""Mímir v12 MemoryProvider — Hermes native memory provider (flat-layout).

Implements the agent.memory_provider.MemoryProvider ABC so the Hermes plugin
loader (plugins/memory/__init__.py) can discover it at
$HERMES_HOME/plugins/mimir_memory_provider/.

Division of labour:
  - WRITE path stays with the mimir-v9.2-cdc collector (conversation ingest
    every 5 min); this provider does NOT re-ingest turns to avoid duplicate
    extraction runs.
  - READ path lives here: prefetch() injects Mímir recall into every turn and
    the mimir_* tools let the agent search/remember explicitly.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .tools import (
    ADMIN_TOKEN_FILE,
    MIMIR_API,
    mimir_recent,
    mimir_reflect,
    mimir_remember,
    mimir_search,
)

logger = logging.getLogger(__name__)

MAX_PREFETCH_RESULTS = 5
MAX_PREFETCH_CHARS = 1400


def _format_results(results: list[dict]) -> str:
    if not results:
        return ""
    lines = []
    for fact in results[:MAX_PREFETCH_RESULTS]:
        text = (fact.get("summary") or fact.get("content") or "").strip()
        if not text:
            continue
        lines.append(f"- {text[:300]}")
        if sum(len(l) for l in lines) > MAX_PREFETCH_CHARS:
            break
    if not lines:
        return ""
    return "Mímir memory recall (trusted long-term facts):\n" + "\n".join(lines)


SEARCH_SCHEMA = {
    "name": "mimir_search",
    "description": (
        "Search Mímir long-term memory for facts relevant to a query. "
        "Governed, event-sourced facts (verified knowledge), not raw chat history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language query."},
            "limit": {"type": "integer", "description": "Max results (default 5)."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "mimir_remember",
    "description": (
        "Persist a durable fact into Mímir (goes through the governance "
        "pipeline before becoming active)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Fact content."},
            "domain": {
                "type": "string",
                "enum": ["infrastructure", "quant", "tech_support", "personal",
                         "system", "knowledge"],
                "description": "Knowledge domain (default personal).",
            },
            "fact_type": {
                "type": "string",
                "enum": ["user_pref", "project_config", "event", "pattern",
                         "iron_rule", "reference"],
                "description": "Fact type (default user_pref).",
            },
        },
        "required": ["content"],
    },
}

RECENT_SCHEMA = {
    "name": "mimir_recent",
    "description": "Return the most recently recorded Mímir memories.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        },
    },
}

REFLECT_SCHEMA = {
    "name": "mimir_reflect",
    "description": (
        "Reflect on Mímir knowledge: with a topic, gather related facts; "
        "without one, return the retrieval-quality evolution report."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Optional topic to reflect on."},
        },
    },
}


class MimirMemoryProvider(MemoryProvider):
    """Mímir federated memory as a Hermes MemoryProvider."""

    def __init__(self) -> None:
        self._session_id = ""
        self._platform = ""
        self._agent_identity = ""

    @property
    def name(self) -> str:
        return "mimir"

    def is_available(self) -> bool:
        return ADMIN_TOKEN_FILE.exists()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        self._platform = str(kwargs.get("platform") or "")
        self._agent_identity = str(kwargs.get("agent_identity") or "")
        logger.info(
            "Mímir memory provider initialized (api=%s session=%s platform=%s)",
            MIMIR_API, self._session_id, self._platform,
        )

    def system_prompt_block(self) -> str:
        return (
            "Long-term memory is served by Mímir (federated, governed facts). "
            "Relevant facts are auto-recalled each turn; use mimir_search / "
            "mimir_remember for explicit deep recall or durable remembering."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or not query.strip():
            return ""
        try:
            results = mimir_search(query, limit=MAX_PREFETCH_RESULTS)
        except Exception as exc:  # fail closed — never break the agent loop
            logger.debug("mimir prefetch failed: %s", exc)
            return ""
        return _format_results(results)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, REMEMBER_SCHEMA, RECENT_SCHEMA, REFLECT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        owner = self._owner()
        try:
            if tool_name == "mimir_search":
                out = mimir_search(str(args.get("query", "")),
                                   limit=int(args.get("limit", 5)))
            elif tool_name == "mimir_remember":
                out = mimir_remember(
                    str(args.get("content", "")),
                    owner=owner,
                    domain=str(args.get("domain", "personal")),
                    fact_type=str(args.get("fact_type", "user_pref")),
                )
            elif tool_name == "mimir_recent":
                out = mimir_recent(limit=int(args.get("limit", 10)))
            elif tool_name == "mimir_reflect":
                out = mimir_reflect(topic=str(args.get("topic", "")))
            else:
                return json.dumps({"error": f"unknown tool {tool_name}"})
        except Exception as exc:
            logger.warning("mimir tool %s failed: %s", tool_name, exc)
            return json.dumps({"error": str(exc)})
        return json.dumps(out if out is not None else {}, ensure_ascii=False)

    def _owner(self) -> str:
        return os.environ.get("MIMIR_PLUGIN_OWNER", "") or self._agent_identity or "mentor"

    def backup_paths(self) -> List[str]:
        return []
