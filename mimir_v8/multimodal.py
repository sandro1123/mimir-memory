"""Multi-modal asset references for Mímir (M4).

A canonical fact may reference external media assets (screenshot, image,
audio, document, file) without inventing a second vector space: the fact's
text still drives retrieval, while the asset reference is stored alongside
and surfaced in search results and Obsidian publication as an `![[embed]]`.

Policy (applies per fact):
- only _published_ facts (visibility != owner_only and egress != local_only)
  may carry assets destined for Obsidian publication
- assets are additive: attaching never rewrites the fact's event history
- asset references are audited with a memory_event per attachment

Design principle: like every other Mímir evolution, attaching an asset only
appends rows and events — history is never mutated.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .store import CanonicalStore, new_id, sha256_text, utc_now

V18_ADDITIVE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS fact_assets (
        asset_id TEXT PRIMARY KEY,
        fact_id TEXT NOT NULL REFERENCES facts(fact_id),
        asset_kind TEXT NOT NULL CHECK (asset_kind IN ('image','audio','document','file')),
        asset_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        actor_principal TEXT NOT NULL
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_fact_assets_fact
       ON fact_assets(fact_id, created_at)""",
)

# Asset kinds Obsidian can embed inline.
_EMBEDDABLE = frozenset({"image", "audio"})

# Facts must satisfy both conditions before an asset may be attached.
_PUBLISHABLE = {"all", "shared"}
_EGRESS_OK = {"redacted_external", "external_allowed"}


class AssetError(RuntimeError):
    pass


class MultiModalService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def attach(self, fact_id: str, asset_kind: str, asset_ref: str,
               actor_principal: str = "service:multimodal") -> dict:
        """Attach a media asset reference to an active, publishable fact.

        Rejects unknown asset kinds, empty references, and facts whose
        visibility/egress policy forbids publication.
        """
        if asset_kind not in _EMBEDDABLE | {"document", "file"}:
            raise AssetError(f"unsupported asset_kind: {asset_kind!r}")
        if not asset_ref or not asset_ref.strip():
            raise AssetError("asset_ref is required")
        now = utc_now()
        asset_id = new_id()
        with self.store.transaction() as connection:
            fact = connection.execute(
                "SELECT * FROM facts WHERE fact_id=?", (fact_id,)
            ).fetchone()
            if not fact:
                raise AssetError(f"unknown fact: {fact_id}")
            if fact["status"] != "active":
                raise AssetError(f"fact {fact_id} is not active")
            if fact["visibility"] not in _PUBLISHABLE:
                raise AssetError(
                    f"owner_only fact {fact_id} cannot carry published assets"
                )
            if fact["egress_policy"] not in _EGRESS_OK:
                raise AssetError(
                    f"local_only fact {fact_id} cannot carry published assets"
                )
            connection.execute(
                """INSERT INTO fact_assets(
                    asset_id, fact_id, asset_kind, asset_ref,
                    created_at, actor_principal
                ) VALUES(?,?,?,?,?,?)""",
                (asset_id, fact_id, asset_kind, asset_ref.strip(), now,
                 actor_principal),
            )
            connection.execute(
                """INSERT INTO memory_events(
                    event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                    actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), "fact", fact_id, 1, "fact.asset_attached",
                 actor_principal, new_id(), new_id(), now,
                 json.dumps({"asset_id": asset_id, "asset_kind": asset_kind,
                             "asset_ref": asset_ref.strip()}),
                 sha256_text(f"{fact_id}:{asset_id}:{asset_kind}")),
            )
        return {"status": "ok", "asset_id": asset_id, "fact_id": fact_id}

    def list(self, fact_id: str) -> list[dict]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT asset_id, asset_kind, asset_ref, created_at,
                          actor_principal
                FROM fact_assets WHERE fact_id=? ORDER BY created_at""",
                (fact_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def embed_ref(self, asset: dict) -> str:
        """Render an Obsidian embed reference for an attachable asset."""
        target = asset.get("asset_ref", "").strip()
        if not target:
            return ""
        if asset.get("asset_kind") in _EMBEDDABLE:
            return f"![[{target}]]"
        return f"[[{target}]]"


def asset_note(assets: list[dict]) -> str:
    """Render an Obsidian '## 附件 (multi-modal)' section for a fact note."""
    if not assets:
        return ""
    lines = ["", "## 附件 (multi-modal)"]
    for asset in assets:
        ref = asset.get("asset_ref", "").strip()
        kind = asset.get("asset_kind", "file")
        if kind in ("image", "audio"):
            lines.append(f"![[{ref}]]")
        else:
            lines.append(f"- [[{ref}]] ({kind})")
    return "\n".join(lines) if len(lines) > 1 else ""


def asset_to_context(assets: list[dict]) -> list[dict]:
    """Shape assets for API/query results (no raw embedding lists)."""
    return [
        {"asset_id": a["asset_id"], "kind": a["asset_kind"],
         "asset_ref": a["asset_ref"], "created_at": a["created_at"]}
        for a in assets
    ]