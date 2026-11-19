"""Agent adapter contract, validation, and in-process registry (1.8.0 spec §4).

An adapter describes *how an agent harness talks to Mímir* (lifecycle hooks,
endpoint usage, payload conventions). Adapters are client-side shims over
stable backend endpoints; they never redefine source governance — that is the
source registry's concern (``mimir_v8.sources``).

Contract source of truth: ``docs/superpowers/specs/2026-04-11-mimir-1.8-agent-adapter-and-memory-source-extensibility-design.md`` §4.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from mimir_v8.schema import RETENTION_CLASSES, ValidationError

TIER = Literal["A", "B", "C"]
ADAPTER_STATUS = Literal["builtin", "experimental", "deprecated"]

_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_ENDPOINT_METHODS = frozenset({"GET", "POST"})


@dataclass(frozen=True)
class EndpointBinding:
    """A stable HTTP binding an adapter steers clients to."""

    method: str
    path: str

    def __post_init__(self) -> None:
        if self.method not in _ENDPOINT_METHODS:
            raise ValidationError(f"invalid endpoint method: {self.method!r}")
        if not self.path.startswith("/"):
            raise ValidationError(f"endpoint path must start with '/': {self.path!r}")


@dataclass(frozen=True)
class AdapterCapabilities:
    """Declared capability set (spec §4.2). All booleans default false."""

    session_start: bool = False
    session_end: bool = False
    post_tool_use: bool = False
    trajectory_capture: bool = False
    explicit_remember_assist: bool = False
    supports_incremental_flush: bool = False

    def minimum_viable(self) -> bool:
        """At least one of the three viable surfaces must be declared."""
        return (
            self.session_start
            or self.session_end
            or self.explicit_remember_assist
        )


@dataclass(frozen=True)
class IdempotencyStrategy:
    """How replay-safe keys are formed for this adapter's submissions."""

    key_strategy: Literal["session_end", "message_range", "external_event", "custom"]
    key_template: str | None = None


@dataclass(frozen=True)
class AdapterSpec:
    """Validated description of one agent/harness adapter (spec §4.2)."""

    adapter_id: str
    display_name: str
    version: str
    tier: TIER
    status: ADAPTER_STATUS
    description: str
    capabilities: AdapterCapabilities
    hooks_start: EndpointBinding | None = None
    hooks_end: EndpointBinding | None = None
    default_connector_type: str = "external_agent"
    connector_id_template: str = "{adapter_id}:{runtime_instance}"
    session_id_source: Literal["runtime", "generated", "none"] = "runtime"
    default_memory_mode: Literal["observe", "never"] = "observe"
    default_retention_class: str = "short"
    owner_principal_mode: Literal["authenticated_principal", "explicit_body", "mapped"] = "authenticated_principal"
    idempotency: IdempotencyStrategy = field(default_factory=lambda: IdempotencyStrategy("session_end"))
    metadata: dict[str, Any] = field(default_factory=dict)


def validate_adapter_spec(spec: AdapterSpec) -> None:
    """Fail closed on any contract violation; safe specs pass silently."""
    if not isinstance(spec.adapter_id, str) or not _ID_PATTERN.match(spec.adapter_id or ""):
        raise ValidationError(
            f"adapter_id must be kebab-case ([a-z0-9-]+), got {spec.adapter_id!r}"
        )
    if not spec.display_name or not spec.description:
        raise ValidationError("display_name and description are required")
    if not spec.version:
        raise ValidationError("adapter contract version is required")
    if spec.tier not in ("A", "B", "C"):
        raise ValidationError(f"tier must be A|B|C, got {spec.tier!r}")
    if spec.status not in ("builtin", "experimental", "deprecated"):
        raise ValidationError(f"invalid status: {spec.status!r}")
    if not spec.capabilities.minimum_viable():
        raise ValidationError(
            "adapter must declare at least one of session_start/session_end/"
            "explicit_remember_assist"
        )
    # Hook capability requires the matching endpoint binding; the default
    # paths are the only ones 1.8 accepts (additive endpoints stay unused).
    if spec.capabilities.session_start:
        if spec.hooks_start is None:
            raise ValidationError("session_start capability requires hooks_start binding")
        if spec.hooks_start.path != "/v8/hooks/session-start":
            raise ValidationError(
                f"hooks_start must target /v8/hooks/session-start, got {spec.hooks_start.path!r}"
            )
    else:
        if spec.hooks_start is not None:
            raise ValidationError("hooks_start set without session_start capability")
    if spec.capabilities.session_end:
        if spec.hooks_end is None:
            raise ValidationError("session_end capability requires hooks_end binding")
        if spec.hooks_end.path != "/v8/hooks/session-end":
            raise ValidationError(
                f"hooks_end must target /v8/hooks/session-end, got {spec.hooks_end.path!r}"
            )
    else:
        if spec.hooks_end is not None:
            raise ValidationError("hooks_end set without session_end capability")
    if spec.tier == "A" and not (spec.capabilities.session_start or spec.capabilities.session_end):
        raise ValidationError("A-tier adapters must declare hook lifecycle capability")
    # Explicit memory mode is forbidden on hook auto paths (spec §4.2/§4.5).
    if spec.default_memory_mode not in ("observe", "never"):
        raise ValidationError(
            "default_memory_mode must be observe|never; explicit belongs to remember()"
        )
    if spec.default_retention_class not in RETENTION_CLASSES:
        raise ValidationError(
            f"default_retention_class {spec.default_retention_class!r} not in "
            f"{sorted(RETENTION_CLASSES)}"
        )
    if spec.owner_principal_mode not in ("authenticated_principal", "explicit_body", "mapped"):
        raise ValidationError(f"invalid owner_principal_mode: {spec.owner_principal_mode!r}")
    if spec.idempotency.key_strategy not in ("session_end", "message_range", "external_event", "custom"):
        raise ValidationError(f"invalid idempotency strategy: {spec.idempotency.key_strategy!r}")
    if spec.idempotency.key_strategy == "custom" and not spec.idempotency.key_template:
        raise ValidationError("custom idempotency strategy requires key_template")


_HOOKS_START = EndpointBinding("GET", "/v8/hooks/session-start")
_HOOKS_END = EndpointBinding("POST", "/v8/hooks/session-end")


def _builtin_specs() -> dict[str, AdapterSpec]:
    """The pre-1.8 hardcoded pair, expressed as validated specs."""
    hooks_caps = AdapterCapabilities(session_start=True, session_end=True)
    return {
        "claude-code": AdapterSpec(
            adapter_id="claude-code",
            display_name="Claude Code",
            version="1.0.0",
            tier="A",
            status="builtin",
            description="Anthropic Claude Code harness via SessionStart/SessionEnd hooks.",
            capabilities=hooks_caps,
            hooks_start=_HOOKS_START,
            hooks_end=_HOOKS_END,
            default_memory_mode="observe",
            default_retention_class="short",
        ),
        "codex": AdapterSpec(
            adapter_id="codex",
            display_name="Codex",
            version="1.0.0",
            tier="A",
            status="builtin",
            description="OpenAI Codex harness via SessionStart/SessionEnd hooks.",
            capabilities=hooks_caps,
            hooks_start=_HOOKS_START,
            hooks_end=_HOOKS_END,
            default_memory_mode="observe",
            default_retention_class="short",
        ),
    }


class AdapterRegistry:
    """In-process adapter registry; strict, fail-closed registration."""

    def __init__(self) -> None:
        self._specs: dict[str, AdapterSpec] = {}

    def register(self, spec: AdapterSpec, *, override: bool = False) -> None:
        validate_adapter_spec(spec)
        existing = self._specs.get(spec.adapter_id)
        if existing is not None and not override:
            raise ValidationError(
                f"adapter {spec.adapter_id!r} already registered; pass override=True to replace"
            )
        self._specs[spec.adapter_id] = spec

    def get(self, adapter_id: str) -> AdapterSpec | None:
        return self._specs.get(adapter_id)

    def require(self, adapter_id: str) -> AdapterSpec:
        spec = self._specs.get(adapter_id)
        if spec is None:
            raise ValidationError(f"adapter {adapter_id!r} is not registered")
        return spec

    def list(self, *, tier: TIER | None = None, capability: str | None = None) -> list[AdapterSpec]:
        """Registered specs, sorted by adapter_id for deterministic output."""
        if capability is not None and not hasattr(AdapterCapabilities, capability):
            raise ValidationError(f"unknown capability: {capability!r}")
        result = []
        for spec in self._specs.values():
            if tier is not None and spec.tier != tier:
                continue
            if capability is not None and not getattr(spec.capabilities, capability):
                continue
            result.append(spec)
        return sorted(result, key=lambda s: s.adapter_id)


_REGISTRY: AdapterRegistry | None = None


def get_registry() -> AdapterRegistry:
    """Process-wide registry, bootstrapped with the builtin adapters."""
    global _REGISTRY
    if _REGISTRY is None:
        registry = AdapterRegistry()
        for spec in _builtin_specs().values():
            registry.register(spec)
        _REGISTRY = registry
    return _REGISTRY


def register_adapter(spec: AdapterSpec, *, override: bool = False) -> None:
    get_registry().register(spec, override=override)


def get_adapter(adapter_id: str) -> AdapterSpec | None:
    return get_registry().get(adapter_id)


def require_adapter(adapter_id: str) -> AdapterSpec:
    return get_registry().require(adapter_id)


def list_adapters(*, tier: TIER | None = None, capability: str | None = None) -> list[AdapterSpec]:
    return get_registry().list(tier=tier, capability=capability)


def manifest(spec: AdapterSpec) -> dict[str, Any]:
    """Non-sensitive manifest summary (introspection payloads, docs, 1.9 installer)."""
    return {
        "adapter_id": spec.adapter_id,
        "display_name": spec.display_name,
        "version": spec.version,
        "tier": spec.tier,
        "status": spec.status,
        "description": spec.description,
        "capabilities": {
            "session_start": spec.capabilities.session_start,
            "session_end": spec.capabilities.session_end,
            "post_tool_use": spec.capabilities.post_tool_use,
            "trajectory_capture": spec.capabilities.trajectory_capture,
            "explicit_remember_assist": spec.capabilities.explicit_remember_assist,
            "supports_incremental_flush": spec.capabilities.supports_incremental_flush,
        },
        "hooks": {
            "start": {"method": spec.hooks_start.method, "path": spec.hooks_start.path} if spec.hooks_start else None,
            "end": {"method": spec.hooks_end.method, "path": spec.hooks_end.path} if spec.hooks_end else None,
        },
        "default_connector_type": spec.default_connector_type,
        "connector_id_template": spec.connector_id_template,
        "session_id_source": spec.session_id_source,
        "default_memory_mode": spec.default_memory_mode,
        "default_retention_class": spec.default_retention_class,
        "owner_principal_mode": spec.owner_principal_mode,
        "idempotency": {
            "key_strategy": spec.idempotency.key_strategy,
            "key_template": spec.idempotency.key_template,
        },
        "metadata": dict(spec.metadata),
    }
