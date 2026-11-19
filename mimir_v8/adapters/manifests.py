"""Declarative adapter/source manifest loading (1.8.0 spec §9 Task 3, §4.3, §5.3).

Loads YAML/JSON adapter and source manifests from a configured directory
(``MIMIR_ADAPTERS_DIR`` / ``MIMIR_SOURCES_DIR``) or explicit path. Strict and
fail closed: one invalid manifest aborts the whole load (nothing registers).
Default behavior with no env/config is a no-op — dynamic plugins/entry-points
stay off by default (supply-chain posture, spec §8).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mimir_v8.adapters.registry import (
    AdapterCapabilities,
    AdapterSpec,
    EndpointBinding,
    IdempotencyStrategy,
    register_adapter,
)
from mimir_v8.schema import ValidationError
from mimir_v8.sources.registry import SourceSpec, register_source

_ADAPTERS_ENV = "MIMIR_ADAPTERS_DIR"
_SOURCES_ENV = "MIMIR_SOURCES_DIR"
_MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")


def _parse_structured_file(path: Path) -> dict[str, Any]:
    """Parse one YAML/JSON manifest file into a dict; any failure raises."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise ValidationError(
                f"cannot load {path.name}: PyYAML is not installed; use JSON manifests"
            ) from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValidationError(f"manifest {path.name} must be a mapping at the top level")
    return data


def _require_str(data: dict[str, Any], key: str, origin: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{origin}: field {key!r} must be a non-empty string")
    return value


def adapter_spec_from_manifest(data: dict[str, Any], *, origin: str) -> AdapterSpec:
    """Build an AdapterSpec from a manifest dict; strict, fail closed."""
    hooks = data.get("hooks") or {}
    if not isinstance(hooks, dict):
        raise ValidationError(f"{origin}: 'hooks' must be a mapping")
    hooks_start = None
    hooks_end = None
    if isinstance(hooks.get("start"), dict):
        hooks_start = EndpointBinding(hooks["start"].get("method", "GET"), hooks["start"]["path"])
    if isinstance(hooks.get("end"), dict):
        hooks_end = EndpointBinding(hooks["end"].get("method", "POST"), hooks["end"]["path"])
    caps = data.get("capabilities") or {}
    if not isinstance(caps, dict):
        raise ValidationError(f"{origin}: 'capabilities' must be a mapping")
    capabilities = AdapterCapabilities(
        session_start=bool(caps.get("session_start", False)),
        session_end=bool(caps.get("session_end", False)),
        post_tool_use=bool(caps.get("post_tool_use", False)),
        trajectory_capture=bool(caps.get("trajectory_capture", False)),
        explicit_remember_assist=bool(caps.get("explicit_remember_assist", False)),
        supports_incremental_flush=bool(caps.get("supports_incremental_flush", False)),
    )
    idempotency = data.get("idempotency") or {}
    if not isinstance(idempotency, dict):
        raise ValidationError(f"{origin}: 'idempotency' must be a mapping")
    idempotency_strategy = IdempotencyStrategy(
        key_strategy=idempotency.get("key_strategy", "session_end"),
        key_template=idempotency.get("key_template"),
    )
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValidationError(f"{origin}: 'metadata' must be a mapping")
    spec = AdapterSpec(
        adapter_id=_require_str(data, "adapter_id", origin),
        display_name=_require_str(data, "display_name", origin),
        version=_require_str(data, "version", origin),
        tier=data.get("tier", "C"),
        status=data.get("status", "experimental"),
        description=_require_str(data, "description", origin),
        capabilities=capabilities,
        hooks_start=hooks_start,
        hooks_end=hooks_end,
        default_connector_type=data.get("default_connector_type", "external_agent"),
        connector_id_template=data.get("connector_id_template", "{adapter_id}:{runtime_instance}"),
        session_id_source=data.get("session_id_source", "runtime"),
        default_memory_mode=data.get("default_memory_mode", "observe"),
        default_retention_class=data.get("default_retention_class", "short"),
        owner_principal_mode=data.get("owner_principal_mode", "authenticated_principal"),
        idempotency=idempotency_strategy,
        metadata=metadata,
    )
    # normalize_adapter_spec runs the full contract validation; manifest
    # errors surface here, before anything is registered.
    return validate_and_return(spec, origin)


def validate_and_return(spec: AdapterSpec, origin: str) -> AdapterSpec:
    from mimir_v8.adapters.registry import validate_adapter_spec

    validate_adapter_spec(spec)
    return spec


def source_spec_from_manifest(data: dict[str, Any], *, origin: str) -> SourceSpec:
    """Build a SourceSpec from a manifest dict; strict, fail closed."""
    from mimir_v8.sources.registry import validate_source_spec

    allowed_modes = data.get("allowed_memory_modes") or ["observe", "never"]
    if not isinstance(allowed_modes, list) or not all(isinstance(m, str) for m in allowed_modes):
        raise ValidationError(f"{origin}: 'allowed_memory_modes' must be a list of strings")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValidationError(f"{origin}: 'metadata' must be a mapping")
    spec = SourceSpec(
        connector_type=_require_str(data, "connector_type", origin),
        display_name=_require_str(data, "display_name", origin),
        description=_require_str(data, "description", origin),
        source_category=data.get("source_category", "unknown"),
        ingest_kind=data.get("ingest_kind", "conversation"),
        interaction_mode=data.get("interaction_mode", "push"),
        status=data.get("status", "experimental"),
        supports_incremental=bool(data.get("supports_incremental", False)),
        supports_delete_sync=bool(data.get("supports_delete_sync", False)),
        supports_schedule=bool(data.get("supports_schedule", False)),
        allowed_memory_modes=frozenset(allowed_modes),
        default_memory_mode=data.get("default_memory_mode", "observe"),
        default_retention_class=data.get("default_retention_class", "standard"),
        auto_candidate=bool(data.get("auto_candidate", False)),
        extraction_policy=data.get("extraction_policy", "none"),
        idempotency_strategy=data.get("idempotency_strategy", "content_fingerprint"),
        metadata=metadata,
    )
    validate_source_spec(spec)
    return spec


def _load_dir(directory: Path, *, env_name: str) -> list[Path]:
    if not directory.is_dir():
        raise ValidationError(
            f"{env_name}={directory} is not a directory; refusing to load manifests"
        )
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in _MANIFEST_SUFFIXES
    )


def load_adapter_manifests(path: str | Path | None = None) -> list[AdapterSpec]:
    """Load and register adapter manifests; returns the registered specs.

    All-or-nothing: parse/validate every manifest first, register only when
    the entire batch is valid (no partial registration of unsafe config).
    """
    if path is None:
        configured = os.environ.get(_ADAPTERS_ENV, "").strip()
        if not configured:
            return []
        directory = Path(configured)
    else:
        directory = Path(path)

    if not directory.is_dir():
        raise ValidationError(
            f"{_ADAPTERS_ENV}={directory} is not a directory; refusing to load manifests"
        )
    specs = [
        adapter_spec_from_manifest(_parse_structured_file(p), origin=p.name)
        for p in _load_dir(directory, env_name=_ADAPTERS_ENV)
    ]
    for spec in specs:
        register_adapter(spec)
    return specs


def load_source_manifests(path: str | Path | None = None) -> list[SourceSpec]:
    """Load and register source manifests; returns the registered specs."""
    if path is None:
        configured = os.environ.get(_SOURCES_ENV, "").strip()
        if not configured:
            return []
        directory = Path(configured)
    else:
        directory = Path(path)

    if not directory.is_dir():
        raise ValidationError(
            f"{_SOURCES_ENV}={directory} is not a directory; refusing to load manifests"
        )
    specs = [
        source_spec_from_manifest(_parse_structured_file(p), origin=p.name)
        for p in _load_dir(directory, env_name=_SOURCES_ENV)
    ]
    for spec in specs:
        register_source(spec)
    return specs