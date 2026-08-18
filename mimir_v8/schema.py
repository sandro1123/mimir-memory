"""Mímir v9.2 schema constants and command validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

MIMIR_VERSION = "12.0.1"
SCHEMA_VERSION = 18

DECAY_TIERS = frozenset({"L0_never", "L1_preference", "L2_config", "L3_event", "L4_temporary", "L5_ephemeral"})
DECAY_TIER_MAP = {
    "iron_rule": "L0_never",
    "user_pref": "L1_preference",
    "project_config": "L2_config",
    "event": "L3_event",
    "pattern": "L4_temporary",
    "ephemeral": "L5_ephemeral",
    "learning": "L4_temporary",
    "reference": "L4_temporary",
}
DECAY_HALF_LIFE = {
    "L0_never": None,
    "L1_preference": 365,
    "L2_config": 180,
    "L3_event": 90,
    "L4_temporary": 30,
    "L5_ephemeral": 7,
}

MEMORY_MODES = frozenset({"explicit", "observe", "never"})

OPINION_STANCES = frozenset({"support", "oppose", "neutral"})
OPINION_CONFIDENCE_DELTA = 0.1
OPINION_STALE_DAYS = 180
RETENTION_CLASSES = frozenset({"session", "short", "standard", "permanent", "legal_hold"})
CONVERSATION_ROLES = frozenset({"system", "user", "assistant", "tool", "developer", "unknown"})
LEARNING_STATUSES = frozenset({"accepted", "redacted", "rejected", "candidate", "committed"})

AGENT_IDS = frozenset({"heimdallr", "quantmaster", "jarvis", "mentor"})
DOMAINS = frozenset(
    {"infrastructure", "quant", "tech_support", "personal", "system", "knowledge"}
)
FACT_TYPES = frozenset(
    {
        "iron_rule",
        "user_pref",
        "project_config",
        "event",
        "pattern",
        "ephemeral",
        "learning",
        "reference",
    }
)
VISIBILITIES = frozenset({"all", "shared", "owner_only"})
SENSITIVITIES = frozenset({"internal", "confidential", "restricted"})
EGRESS_POLICIES = frozenset({"local_only", "redacted_external", "external_allowed"})
FACT_STATUSES = frozenset({"active", "tombstoned", "disputed", "archived"})
HUMAN_STATUSES = frozenset({"unreviewed", "confirmed", "rejected", "disputed"})
RELATION_TYPES = frozenset(
    {
        "supersedes",
        "contradicts",
        "supports",
        "derived_from",
        "about_entity",
        "belongs_to_project",
        "duplicate_of",
        "merged_into",
    }
)
PROJECTORS = ("vector", "fts", "graph", "core_memory")
PUBLICATION_WORKERS = ("vault_managed_sections",)
VAULT_ADAPTER_PRINCIPAL = "service:vault_adapter"
DOCUMENT_TYPES = frozenset({"decision", "overview", "runbook", "learning", "review_board"})
DOCUMENT_STATUSES = frozenset({"active", "review", "archived"})
DOCUMENT_TARGET_SCOPES = frozenset({"private", "shared"})


class ValidationError(ValueError):
    """Raised when a command violates the current canonical schema."""


def _required_text(name: str, value: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be non-empty text")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ValidationError(f"{name} exceeds {max_length} characters")
    return cleaned


def _choice(name: str, value: str, choices: Iterable[str]) -> str:
    if value not in choices:
        raise ValidationError(f"invalid {name}: {value}")
    return value


@dataclass(frozen=True)
class UpdateFact:
    fact_id: str
    expected_version: int
    content: str | None = None
    summary: str | None = None
    human_status: str | None = None
    confidence_score: float | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    change_reason: str = "canonical fact updated"
    idempotency_key: str | None = None

    def validated(self) -> "UpdateFact":
        fact_id = _required_text("fact_id", self.fact_id, 128)
        if self.expected_version < 1:
            raise ValidationError("expected_version must be at least 1")
        content = None if self.content is None else _required_text("content", self.content, 100_000)
        summary = self.summary.strip() if isinstance(self.summary, str) else None
        if summary is not None and (not summary or len(summary) > 2_000):
            raise ValidationError("summary must contain 1 to 2000 characters")
        if self.human_status is not None:
            _choice("human_status", self.human_status, HUMAN_STATUSES)
        if self.confidence_score is not None and not 0 <= self.confidence_score <= 1:
            raise ValidationError("confidence_score must be between 0 and 1")
        if all(
            value is None
            for value in (
                content,
                summary,
                self.human_status,
                self.confidence_score,
                self.valid_from,
                self.valid_to,
            )
        ):
            raise ValidationError("update must change at least one field")
        return UpdateFact(
            fact_id=fact_id,
            expected_version=self.expected_version,
            content=content,
            summary=summary,
            human_status=self.human_status,
            confidence_score=self.confidence_score,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            change_reason=_required_text("change_reason", self.change_reason, 2_000),
            idempotency_key=self.idempotency_key.strip() if self.idempotency_key else None,
        )


@dataclass(frozen=True)
class TombstoneFact:
    fact_id: str
    expected_version: int
    reason: str
    idempotency_key: str | None = None

    def validated(self) -> "TombstoneFact":
        if self.expected_version < 1:
            raise ValidationError("expected_version must be at least 1")
        return TombstoneFact(
            fact_id=_required_text("fact_id", self.fact_id, 128),
            expected_version=self.expected_version,
            reason=_required_text("reason", self.reason, 2_000),
            idempotency_key=self.idempotency_key.strip() if self.idempotency_key else None,
        )


@dataclass(frozen=True)
class GrantFactAccess:
    fact_id: str
    subject_type: str
    subject_id: str
    permission: str = "read"
    effect: str = "allow"
    expires_at: str | None = None
    idempotency_key: str | None = None

    def validated(self) -> "GrantFactAccess":
        if self.subject_type not in {"principal", "role", "project_role"}:
            raise ValidationError(f"invalid subject_type: {self.subject_type}")
        if self.permission not in {"read", "write", "review", "delete", "export", "manage"}:
            raise ValidationError(f"invalid permission: {self.permission}")
        if self.effect not in {"allow", "deny"}:
            raise ValidationError(f"invalid effect: {self.effect}")
        return GrantFactAccess(
            fact_id=_required_text("fact_id", self.fact_id, 128),
            subject_type=self.subject_type,
            subject_id=_required_text("subject_id", self.subject_id, 128),
            permission=self.permission,
            effect=self.effect,
            expires_at=self.expires_at,
            idempotency_key=self.idempotency_key.strip() if self.idempotency_key else None,
        )


@dataclass(frozen=True)
class CreateFact:
    content: str
    owner_principal: str
    domain: str
    fact_type: str
    summary: str | None = None
    visibility: str = "all"
    sensitivity: str = "internal"
    egress_policy: str = "local_only"
    project_id: str | None = None
    human_status: str = "unreviewed"
    confidence_score: float | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    recorded_at: str | None = None
    last_verified_at: str | None = None
    legacy_id: str | None = None
    event_type: str = "fact.created"
    source_kind: str | None = None
    source_uri: str | None = None
    source_hash: str | None = None
    idempotency_key: str | None = None

    def validated(self) -> "CreateFact":
        content = _required_text("content", self.content, 100_000)
        owner = _choice("owner_principal", self.owner_principal, AGENT_IDS)
        domain = _choice("domain", self.domain, DOMAINS)
        fact_type = _choice("fact_type", self.fact_type, FACT_TYPES)
        visibility = _choice("visibility", self.visibility, VISIBILITIES)
        sensitivity = _choice("sensitivity", self.sensitivity, SENSITIVITIES)
        egress_policy = _choice("egress_policy", self.egress_policy, EGRESS_POLICIES)
        human_status = _choice("human_status", self.human_status, HUMAN_STATUSES)
        event_type = _choice("event_type", self.event_type, {"fact.created", "fact.migrated"})
        summary = self.summary.strip() if isinstance(self.summary, str) else content[:200]
        if not summary:
            summary = content[:200]
        if len(summary) > 2_000:
            raise ValidationError("summary exceeds 2000 characters")
        if self.confidence_score is not None and not 0 <= self.confidence_score <= 1:
            raise ValidationError("confidence_score must be between 0 and 1")
        if sensitivity == "restricted" and egress_policy != "local_only":
            raise ValidationError("restricted facts must use local_only egress")
        if fact_type == "ephemeral" and human_status == "confirmed":
            raise ValidationError("ephemeral facts cannot be human-confirmed canonical facts")
        return CreateFact(
            content=content,
            owner_principal=owner,
            domain=domain,
            fact_type=fact_type,
            summary=summary,
            visibility=visibility,
            sensitivity=sensitivity,
            egress_policy=egress_policy,
            project_id=self.project_id.strip() if self.project_id else None,
            human_status=human_status,
            confidence_score=self.confidence_score,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            recorded_at=self.recorded_at,
            last_verified_at=self.last_verified_at,
            legacy_id=self.legacy_id.strip() if self.legacy_id else None,
            event_type=event_type,
            source_kind=self.source_kind.strip() if self.source_kind else None,
            source_uri=self.source_uri.strip() if self.source_uri else None,
            source_hash=self.source_hash.strip() if self.source_hash else None,
            idempotency_key=self.idempotency_key.strip() if self.idempotency_key else None,
        )
