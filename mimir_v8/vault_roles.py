"""Declarative document-role table for the Obsidian vault.

Roles decide how Mímir treats a note: which notes become crystallized
entities, which are archived-only, and which are never read at all.
The table is data (``vault_roles.yaml``), not code, so new vault areas
can be classified without touching Python.

Resolution order (first match wins):
    reference_only prefix -> path prefix -> filename pattern
    -> frontmatter type -> default

The default role is deliberately the most conservative one (``unknown``):
mis-classifying a note as ``topic`` would crystallize it, while
mis-classifying it as ``unknown`` merely under-uses it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROLE_HUB = "hub"
ROLE_TOPIC = "topic"
ROLE_VERSIONED = "versioned"
ROLE_WORKLOG = "worklog"
ROLE_MACHINE_LOG = "machine-log"
ROLE_REFERENCE_ONLY = "reference-only"
ROLE_RAW = "raw"
ROLE_UNKNOWN = "unknown"

DEFAULT_TABLE_PATH = Path(__file__).resolve().parent / "vault_roles.yaml"


def load_role_table(path: Path | None = None) -> dict:
    """Load and normalise the role table.

    Raises:
        FileNotFoundError: when the table file is absent.
    """
    target = Path(path) if path is not None else DEFAULT_TABLE_PATH
    if not target.is_file():
        raise FileNotFoundError(f"role table not found: {target}")

    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}

    patterns: list[tuple[re.Pattern[str], str]] = []
    for entry in raw.get("by_filename_pattern") or []:
        patterns.append((re.compile(entry["pattern"]), entry["role"]))

    return {
        "reference_only": tuple(raw.get("reference_only") or ()),
        "by_path_prefix": dict(raw.get("by_path_prefix") or {}),
        "by_filename_pattern": tuple(patterns),
        "frontmatter_type": dict(raw.get("frontmatter_type") or {}),
        "default": raw.get("default", ROLE_UNKNOWN),
    }


def resolve_role(
    relpath: str, frontmatter: dict[str, str], table: dict
) -> tuple[str, str]:
    """Return ``(role, reason)`` for a vault-relative path.

    ``reason`` names the rule that fired, so a wrong classification can
    be traced back to the table entry that caused it.
    """
    normalised = relpath.replace("\\", "/")

    for prefix in table["reference_only"]:
        if normalised.startswith(prefix):
            return ROLE_REFERENCE_ONLY, f"reference_only:{prefix}"

    for prefix, role in table["by_path_prefix"].items():
        if normalised.startswith(prefix):
            return role, f"path_prefix:{prefix}"

    name = normalised.rsplit("/", 1)[-1]
    for pattern, role in table["by_filename_pattern"]:
        if pattern.search(name):
            return role, f"filename:{pattern.pattern}"

    fm_type = (frontmatter or {}).get("type")
    if fm_type and fm_type in table["frontmatter_type"]:
        return table["frontmatter_type"][fm_type], f"frontmatter_type:{fm_type}"

    return table["default"], "default"
