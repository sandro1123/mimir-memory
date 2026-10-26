"""Double-layer crystallization pipeline for Mímir v1.6.0 Step 3.

Given the externally-sensed source items produced by Step 2 (model probes,
GitHub releases, RSS items), this service turns them into:

- **Layer 1 (text output)**: a human-readable morning-brief Markdown;
- **Layer 2 (structured entities)**: deterministic entity cards that are
  auto-ingested into the canonical store (fact + ``entities`` + ``fact_entities``)
  so they become queryable and projectable to Obsidian later.

The extraction is deliberately deterministic (no LLM call) so the same input
always yields the same cards — this keeps the pipeline testable and idempotent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .schema import CreateFact
from .store import CanonicalStore, new_id, sha256_text, utc_now


@dataclass
class SourceItem:
    """One externally-sensed item fed into the crystallization pipeline."""

    kind: str  # "model_probe" | "github_release" | "rss_item"
    source_id: str  # name/repo from sources.yaml
    title: str
    body: str
    url: str | None = None
    captured_at: str | None = None  # ISO 8601
    status: str | None = None  # optional probe status summary


@dataclass
class EntityCard:
    """A structured entity extracted from a SourceItem (Layer 2 unit)."""

    entity_type: str  # "model_card" | "tool_card"
    name: str
    domain: str
    summary: str
    facts: dict = field(default_factory=dict)


@dataclass
class PublishedCard:
    """Outcome row for one ingested card."""

    name: str
    entity_id: str
    fact_id: str
    role: str


class MorningBriefService:
    """Render a morning brief and crystallize its entities into the store."""

    def __init__(self, store: CanonicalStore):
        self.store = store

    # -- Layer 1 -----------------------------------------------------------
    def render_markdown(
        self, items: list[SourceItem], *, as_of: str | None = None
    ) -> str:
        """Render a deterministic, human-readable morning brief.

        Sections are ordered by kind: model probes, GitHub releases, then
        generic feed items. Falls back to an empty-but-valid brief when no
        items are supplied.
        """
        as_of = as_of or utc_now()
        date_part = as_of[:10] if as_of else ""

        probes = [i for i in items if i.kind == "model_probe"]
        releases = [i for i in items if i.kind == "github_release"]
        feeds = [i for i in items if i.kind not in ("model_probe", "github_release")]

        lines: list[str] = []
        lines.append(f"# Mímir 科技早报 · {date_part}")
        lines.append("")
        lines.append("> 由外部信源感知流水线自动生成（v1.6.0 双层结晶）。")
        lines.append("")

        lines.append("## 模型探针")
        if probes:
            for item in probes:
                state = item.status or "unknown"
                lines.append(f"- **{item.source_id}** — {state}")
                if item.body:
                    lines.append(f"  - {item.body}")
                if item.url:
                    lines.append(f"  - 端点: {item.url}")
        else:
            lines.append("- （暂无探针数据）")
        lines.append("")

        lines.append("## GitHub Release")
        if releases:
            for item in releases:
                lines.append(f"- **{item.source_id}**: {item.title}")
                if item.body:
                    lines.append(f"  - {item.body}")
                if item.url:
                    lines.append(f"  - 链接: {item.url}")
        else:
            lines.append("- （暂无 Release）")
        lines.append("")

        if feeds:
            lines.append("## 资讯")
            for item in feeds:
                lines.append(f"- **{item.source_id}**: {item.title}")
                if item.body:
                    lines.append(f"  - {item.body}")
            lines.append("")

        return "\n".join(lines)

    # -- Layer 2 -----------------------------------------------------------
    def extract_cards(self, items: list[SourceItem]) -> list[EntityCard]:
        """Deterministically turn items into structured entity cards.

        - ``model_probe`` -> ``model_card``
        - ``github_release`` / ``rss_item`` -> ``tool_card``
        """
        cards: list[EntityCard] = []
        for item in items:
            if item.kind == "model_probe":
                facts: dict[str, Any] = {
                    "endpoint": item.url,
                    "probe_status": item.status,
                }
                cards.append(
                    EntityCard(
                        entity_type="model_card",
                        name=item.source_id,
                        domain="free_model_pool",
                        summary=item.body or item.title,
                        facts=facts,
                    )
                )
            elif item.kind in ("github_release", "rss_item"):
                facts = {"url": item.url, "released_at": item.captured_at}
                cards.append(
                    EntityCard(
                        entity_type="tool_card",
                        name=item.source_id,
                        domain="dependency_watch",
                        summary=item.body or item.title,
                        facts=facts,
                    )
                )
        return cards

    # -- Closed loop -------------------------------------------------------
    def publish(
        self,
        items: list[SourceItem],
        *,
        actor_principal: str,
        now: str | None = None,
    ) -> dict:
        """Render the brief (Layer 1) and ingest cards (Layer 2).

        Returns ``{brief_fact_id, cards: [PublishedCard...]}``. Re-publishing
        identical items is idempotent: existing entities are updated in place
        rather than duplicated.
        """
        now = now or utc_now()
        brief_md = self.render_markdown(items, as_of=now)

        brief_fact = self.store.create_fact(
            CreateFact(
                content=brief_md,
                summary=f"科技早报 {now[:10]}",
                domain="system",
                fact_type="event",
                owner_principal=actor_principal,
            ),
            actor_principal=actor_principal,
        )

        published: list[PublishedCard] = []
        for card in self.extract_cards(items):
            entity_id, fact_id = self._upsert_card(card, actor_principal, now)
            self.store.link_fact_entity(
                brief_fact["fact_id"],
                entity_id,
                role="crystallized_entity",
                confidence=1.0,
                now=now,
            )
            published.append(
                PublishedCard(
                    name=card.name,
                    entity_id=entity_id,
                    fact_id=fact_id,
                    role="crystallized_entity",
                )
            )

        return {"brief_fact_id": brief_fact["fact_id"], "cards": published}

    def _upsert_card(
        self, card: EntityCard, actor_principal: str, now: str
    ) -> tuple[str, str]:
        """Create or update a card's entity + fact. Idempotent on (name, type)."""
        existing = self._find_entity(card.name, card.entity_type)
        if existing is not None:
            self.store.update_entity(
                existing["entity_id"],
                description=card.summary,
                metadata=card.facts,
                now=now,
            )
            # Reuse the fact already linked to this entity (entity -> fact dir).
            fact_id = self._linked_fact_id(existing["entity_id"])
            if fact_id is not None:
                return existing["entity_id"], fact_id

        entity = self.store.create_entity(
            entity_type=card.entity_type,
            name=card.name,
            domain=card.domain,
            description=card.summary,
            metadata=card.facts,
            sensitivity="internal",
            visibility="all",
            now=now,
        )
        fact = self.store.create_fact(
            CreateFact(
                content=f"{card.name}: {card.summary}",
                summary=card.summary[:200],
                domain="system",
                fact_type="reference",
                owner_principal=actor_principal,
            ),
            actor_principal=actor_principal,
        )
        self.store.link_fact_entity(
            fact["fact_id"], entity["entity_id"], role="crystallized_entity",
            confidence=1.0, now=now,
        )
        return entity["entity_id"], fact["fact_id"]

    def _linked_fact_id(self, entity_id: str) -> str | None:
        """Return the first fact_id linked to an entity (entity -> fact dir)."""
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT fact_id FROM fact_entities WHERE entity_id = ? LIMIT 1",
                (entity_id,),
            ).fetchone()
        return row["fact_id"] if row else None

    def _find_entity(self, name: str, entity_type: str) -> dict | None:
        """Lookup a non-suspended entity by (name, type) for idempotent upsert."""
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT entity_id, entity_type, name, domain, description,
                       metadata_json, sensitivity, visibility, status,
                       valid_from, valid_to, created_at, updated_at, content_hash
                FROM entities
                WHERE name = ? AND entity_type = ?
                  AND status != 'suspended'
                  AND (valid_to IS NULL OR valid_to > ?)
                """,
                (name, entity_type, utc_now()),
            ).fetchone()
        return dict(row) if row else None
