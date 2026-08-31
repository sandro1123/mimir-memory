"""Mímir centralized path configuration.

All paths must be configured through environment variables or explicit config.
Production mode requires explicit configuration; missing values fail closed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MimirPaths:
    home: Path
    config_file: Path
    data_dir: Path
    cache_dir: Path
    secrets_dir: Path
    log_dir: Path
    vault_root: Path
    connector_hermes_state_db: Path | None

    @classmethod
    def from_env(cls, *, production: bool = False) -> "MimirPaths":
        errors: list[str] = []
        home = os.environ.get("MIMIR_HOME", "").strip()
        config_file = os.environ.get("MIMIR_CONFIG_FILE", "").strip()
        data_dir = (
            os.environ.get("MIMIR_DATA_DIR", "").strip()
            or os.environ.get("MIMIR_V8_DATA_DIR", "").strip()
        )
        cache_dir = os.environ.get("MIMIR_CACHE_DIR", "").strip()
        secrets_dir = os.environ.get("MIMIR_SECRETS_DIR", "").strip()
        log_dir = os.environ.get("MIMIR_LOG_DIR", "").strip()
        vault_root = os.environ.get("MIMIR_VAULT_ROOT", "").strip()
        hermes_db = os.environ.get("MIMIR_CONNECTOR_HERMES_STATE_DB", "").strip()

        if production:
            if not home:
                errors.append("MIMIR_HOME")
            if not config_file:
                errors.append("MIMIR_CONFIG_FILE")
            if not data_dir:
                errors.append("MIMIR_DATA_DIR")
            if not cache_dir:
                errors.append("MIMIR_CACHE_DIR")
            if not secrets_dir:
                errors.append("MIMIR_SECRETS_DIR")
            if not log_dir:
                errors.append("MIMIR_LOG_DIR")
            if not vault_root:
                errors.append("MIMIR_VAULT_ROOT")

        if errors:
            raise MimirConfigError(
                "production mode requires explicit path configuration; "
                f"missing variables: {', '.join(errors)}"
            )

        return cls(
            home=Path(home) if home else Path.home() / ".hermes" / "mimir",
            config_file=Path(config_file) if config_file else Path.home() / ".hermes" / "mimir" / "mimir_config.yaml",
            data_dir=Path(data_dir) if data_dir else Path.home() / ".hermes" / "mimir" / "data",
            cache_dir=Path(cache_dir) if cache_dir else Path.home() / ".hermes" / "mimir" / "collect",
            secrets_dir=Path(secrets_dir) if secrets_dir else Path.home() / ".hermes" / "mimir" / "secrets",
            log_dir=Path(log_dir) if log_dir else Path.home() / ".hermes" / "mimir" / "logs",
            vault_root=Path(vault_root) if vault_root else Path.home() / "obsidian-vault" / "Sandro's Vault",
            connector_hermes_state_db=Path(hermes_db) if hermes_db else None,
        )


class MimirConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""

def require_paths(production: bool = True) -> MimirPaths:
    """Load and validate paths. In production mode, missing required
    variables cause a non-zero exit."""
    try:
        return MimirPaths.from_env(production=production)
    except MimirConfigError as e:
        import sys
        print(f"MIMIR_CONFIG_ERROR: {e}", file=sys.stderr)
        sys.exit(1)

def load_federation_registry(config_file: str | Path | None = None) -> dict:
    """Wire the dynamic agent/domain registry from the config file.

    Reads the optional ``federation`` section (``agents`` / ``domains``
    lists) and registers every entry via schema.register_agent /
    register_domain. Missing file or missing section is a no-op so worker
    and server paths can call this unconditionally at boot.

    Raises ValueError on structural errors — a malformed federation
    section must fail loudly, never silently skip (iron rule #12).
    """
    import yaml

    from . import schema

    if config_file is None:
        config_file = MimirPaths.from_env().config_file
    path = Path(config_file)
    if not path.is_file():
        return {"agents": [], "domains": []}

    with path.open("r", encoding="utf-8") as handle:
        body = yaml.safe_load(handle) or {}
    if not isinstance(body, dict):
        raise ValueError(f"config file is not a mapping: {path}")

    section = body.get("federation") or {}
    if not isinstance(section, dict):
        raise ValueError("federation section must be a mapping with agents/domains lists")

    registered: dict[str, list[str]] = {"agents": [], "domains": []}
    for key, register in (("agents", schema.register_agent), ("domains", schema.register_domain)):
        entries = section.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"federation.{key} must be a list")
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(f"federation.{key} entries must be non-empty strings")
            register(entry.strip())
            registered[key].append(entry.strip())
    return registered
