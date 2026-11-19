"""Memory source connector contract, registry, and policy (1.8.0 spec §5).

A connector describes *what kind of memory a payload represents* and how it
is governed: classification, allowed memory modes, extraction eligibility,
idempotency strategy. It is deliberately separate from agent adapters
(harness wiring) and composed with them at the ingest boundary.

Contract source of truth: ``docs/superpowers/specs/2026-04-11-mimir-1.8-agent-adapter-and-memory-source-extensibility-design.md`` §5.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from mimir_v8.schema import MEMORY_MODES, RETENTION_CLASSES, ValidationError

SOURCE_CATEGORY = Literal["conversation", "external_info", "knowledge_doc"]
INGEST_KIND = Literal["conversation", "knowledge_item", "event", "hybrid"]
INTERACTION_MODE = Literal["push", "pull", "stream", "manual"]
EXTRACTION_POLICY = Literal["none", "candidate_only", "governed_review"]
IDEMPOTENCY_STRATEGY = Literal[
    "external_id", "content_fingerprint", "session_turn_range", "source_cursor", "composite"
]

_ID_PATTERN = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")

QUARANTINE_CATEGORY = "unknown/quarantine"


@dataclass(frozen=True)
class SourceSpec:
    """Validated description of one memory source connector (spec §5.2)."""

    connector_type: str
    display_name: str
    description: str
    source_category: SOURCE_CATEGORY
    ingest_kind: INGEST_KIND
    interaction_mode: INTERACTION_MODE = "push"
    status: Literal["builtin", "experimental", "deprecated"] = "builtin"
    supports_incremental: bool = False
    supports_delete_sync: bool = False
    supports_schedule: bool = False
    allowed_memory_modes: frozenset[str] = field(default_factory=lambda: frozenset({"observe", "never"}))
    default_memory_mode: str = "observe"
    default_retention_class: str = "standard"
    auto_candidate: bool = True
    extraction_policy: EXTRACTION_POLICY = "candidate_only"
    idempotency_strategy: IDEMPOTENCY_STRATEGY = "content_fingerprint"
    quarantine_on_unknown: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


def validate_source_spec(spec: SourceSpec) -> None:
    """Fail closed on any contract violation; safe specs pass silently."""
    if not isinstance(spec.connector_type, str) or not _ID_PATTERN.match(spec.connector_type or ""):
        raise ValidationError(
            f"connector_type must be snake_case ([a-z0-9_]+), got {spec.connector_type!r}"
        )
    if not spec.display_name or not spec.description:
        raise ValidationError("display_name and description are required")
    if spec.source_category not in ("conversation", "external_info", "knowledge_doc"):
        raise ValidationError(f"invalid source_category: {spec.source_category!r}")
    if spec.ingest_kind not in ("conversation", "knowledge_item", "event", "hybrid"):
        raise ValidationError(f"invalid ingest_kind: {spec.ingest_kind!r}")
    if spec.interaction_mode not in ("push", "pull", "stream", "manual"):
        raise ValidationError(f"invalid interaction_mode: {spec.interaction_mode!r}")
    if spec.status not in ("builtin", "experimental", "deprecated"):
        raise ValidationError(f"invalid status: {spec.status!r}")
    invalid = set(spec.allowed_memory_modes) - set(MEMORY_MODES)
    if invalid:
        raise ValidationError(f"allowed_memory_modes not a subset of MEMORY_MODES: {sorted(invalid)}")
    if spec.default_memory_mode not in spec.allowed_memory_modes:
        raise ValidationError(
            f"default_memory_mode {spec.default_memory_mode!r} not in allowed_memory_modes"
        )
    # Hook/session auto paths can never default to explicit (spec §5.2); the
    # explicit memory command path is remember(), not conversation ingest.
    if spec.source_category == "conversation" and "explicit" in spec.allowed_memory_modes:
        raise ValidationError(
            "conversation sources may not allow memory_mode='explicit'; "
            "explicit intent belongs to remember()"
        )
    if spec.default_retention_class not in RETENTION_CLASSES:
        raise ValidationError(
            f"default_retention_class {spec.default_retention_class!r} not in "
            f"{sorted(RETENTION_CLASSES)}"
        )
    # Auto-promotion is candidate/review-only, never direct fact (spec §5.5).
    if spec.extraction_policy == "none" and spec.auto_candidate:
        raise ValidationError("auto_candidate requires a candidate/review extraction_policy")
    if spec.extraction_policy not in ("none", "candidate_only", "governed_review"):
        raise ValidationError(f"invalid extraction_policy: {spec.extraction_policy!r}")
    if spec.idempotency_strategy not in (
        "external_id", "content_fingerprint", "session_turn_range", "source_cursor", "composite"
    ):
        raise ValidationError(f"invalid idempotency_strategy: {spec.idempotency_strategy!r}")


def _builtin_specs() -> dict[str, SourceSpec]:
    """Built-ins matching classifier.SOURCE_CATEGORY_MAP exactly (spec §5.3).

    Behavior parity is the point: these reproduce the pre-registry semantics
    — conversations allow observe/never with auto candidate crystallization,
    external_info and knowledge_doc never enter the conversation pipeline.
    """
    def conversation(connector: str, name: str, desc: str) -> SourceSpec:
        return SourceSpec(
            connector_type=connector,
            display_name=name,
            description=desc,
            source_category="conversation",
            ingest_kind="conversation",
            allowed_memory_modes=frozenset({"observe", "never"}),
            default_memory_mode="observe",
            default_retention_class="short",
            auto_candidate=True,
            extraction_policy="candidate_only",
            idempotency_strategy="session_turn_range",
        )

    def external_info(connector: str, name: str, desc: str) -> SourceSpec:
        return SourceSpec(
            connector_type=connector,
            display_name=name,
            description=desc,
            source_category="external_info",
            ingest_kind="knowledge_item",
            allowed_memory_modes=frozenset({"observe", "never"}),
            default_memory_mode="observe",
            default_retention_class="standard",
            auto_candidate=False,
            extraction_policy="none",
            idempotency_strategy="external_id",
        )

    def knowledge_doc(connector: str, name: str, desc: str) -> SourceSpec:
        return SourceSpec(
            connector_type=connector,
            display_name=name,
            description=desc,
            source_category="knowledge_doc",
            ingest_kind="knowledge_item",
            allowed_memory_modes=frozenset({"observe", "never"}),
            default_memory_mode="observe",
            default_retention_class="standard",
            auto_candidate=False,
            extraction_policy="governed_review",
            idempotency_strategy="content_fingerprint",
        )

    return {
        "hermes_cdc": conversation("hermes_cdc", "Hermes CDC", "Hermes gateway continuous-dialectic-capture conversations."),
        "external_agent": conversation("external_agent", "External Agent", "Agent-session transcripts ingested through hooks or HTTP ingest."),
        "workbuddy": conversation("workbuddy", "WorkBuddy", "WorkBuddy app conversations."),
        "rss": external_info("rss", "RSS Feed", "RSS/Atom feed items."),
        "web": external_info("web", "Web Page", "Fetched web page content."),
        "searxng": external_info("searxng", "SearXNG Search", "Private SearXNG search results."),
        "feishu": knowledge_doc("feishu", "Feishu Doc", "Feishu/Lark documents."),
        "file": knowledge_doc("file", "Local File", "Local files (markdown, pdf, …)."),
        "document": knowledge_doc("document", "Document", "Directly submitted documents."),
        "vault": knowledge_doc("vault", "Vault Note", "Obsidian vault notes (machine cards skipped)."),
    }


class SourceRegistry:
    """In-process source registry; strict, fail-closed registration."""

    def __init__(self) -> None:
        self._specs: dict[str, SourceSpec] = {}

    def register(self, spec: SourceSpec, *, override: bool = False) -> None:
        validate_source_spec(spec)
        existing = self._specs.get(spec.connector_type)
        if existing is not None and not override:
            raise ValidationError(
                f"source {spec.connector_type!r} already registered; pass override=True to replace"
            )
        self._specs[spec.connector_type] = spec

    def get(self, connector_type: str) -> SourceSpec | None:
        return self._specs.get(connector_type)

    def require(self, connector_type: str) -> SourceSpec:
        spec = self._specs.get(connector_type)
        if spec is None:
            raise ValidationError(f"source connector {connector_type!r} is not registered")
        return spec

    def list(
        self,
        *,
        category: SOURCE_CATEGORY | None = None,
        ingest_kind: INGEST_KIND | None = None,
    ) -> list[SourceSpec]:
        if category is not None and category not in ("conversation", "external_info", "knowledge_doc"):
            raise ValidationError(f"unknown category: {category!r}")
        if ingest_kind is not None and ingest_kind not in ("conversation", "knowledge_item", "event", "hybrid"):
            raise ValidationError(f"unknown ingest_kind: {ingest_kind!r}")
        result = []
        for spec in self._specs.values():
            if category is not None and spec.source_category != category:
                continue
            if ingest_kind is not None and spec.ingest_kind != ingest_kind:
                continue
            result.append(spec)
        return sorted(result, key=lambda s: s.connector_type)

    def resolve_category(self, connector_type: Any) -> str:
        """Classifier parity: unknown/invalid types resolve to quarantine."""
        if not isinstance(connector_type, str) or not connector_type.strip():
            return QUARANTINE_CATEGORY
        spec = self._specs.get(connector_type.strip())
        return spec.source_category if spec else QUARANTINE_CATEGORY

    def policy_for(self, connector_type: str) -> SourceSpec:
        """Registered policy or the quarantine fallback (fail-closed).

        Unknown types get a maximally restrictive policy: no conversation
        pipeline, no auto candidates, no explicit modes — quarantine first.
        """
        spec = self._specs.get(connector_type.strip() if isinstance(connector_type, str) else "")
        if spec is not None:
            return spec
        return SourceSpec(
            connector_type=connector_type if isinstance(connector_type, str) else "",
            display_name="Quarantined source",
            description="Unregistered connector type; auto-promotion denied (fail-closed).",
            source_category="knowledge_doc",  # not conversation — never enters the memory pipeline
            ingest_kind="event",
            status="experimental",
            allowed_memory_modes=frozenset({"never"}),
            default_memory_mode="never",
            auto_candidate=False,
            extraction_policy="none",
            metadata={"quarantine": True},
        )


_REGISTRY: SourceRegistry | None = None


def get_registry() -> SourceRegistry:
    """Process-wide registry, bootstrapped with the builtin connectors."""
    global _REGISTRY
    if _REGISTRY is None:
        registry = SourceRegistry()
        for spec in _builtin_specs().values():
            registry.register(spec)
        _REGISTRY = registry
    return _REGISTRY


def register_source(spec: SourceSpec, *, override: bool = False) -> None:
    get_registry().register(spec, override=override)


def get_source(connector_type: str) -> SourceSpec | None:
    return get_registry().get(connector_type)


def require_source(connector_type: str) -> SourceSpec:
    return get_registry().require(connector_type)


def list_sources(
    *, category: SOURCE_CATEGORY | None = None, ingest_kind: INGEST_KIND | None = None
) -> list[SourceSpec]:
    return get_registry().list(category=category, ingest_kind=ingest_kind)


def resolve_category(connector_type: Any) -> str:
    return get_registry().resolve_category(connector_type)


def policy_for(connector_type: str) -> SourceSpec:
    return get_registry().policy_for(connector_type)


def manifest(spec: SourceSpec) -> dict[str, Any]:
    """Non-sensitive manifest summary (introspection payloads, docs)."""
    return {
        "connector_type": spec.connector_type,
        "display_name": spec.display_name,
        "description": spec.description,
        "source_category": spec.source_category,
        "ingest_kind": spec.ingest_kind,
        "interaction_mode": spec.interaction_mode,
        "status": spec.status,
        "supports_incremental": spec.supports_incremental,
        "supports_delete_sync": spec.supports_delete_sync,
        "supports_schedule": spec.supports_schedule,
        "allowed_memory_modes": sorted(spec.allowed_memory_modes),
        "default_memory_mode": spec.default_memory_mode,
        "default_retention_class": spec.default_retention_class,
        "auto_candidate": spec.auto_candidate,
        "extraction_policy": spec.extraction_policy,
        "idempotency_strategy": spec.idempotency_strategy,
        "quarantine_on_unknown": spec.quarantine_on_unknown,
        "metadata": dict(spec.metadata),
    }
