"""Obsidian entity card renderer for the Mímir→vault bridge (stage 2).

A card is the only text Mímir ever writes into the vault. Its shape is
fixed by two contracts:

- the frontmatter carries ``mimir_generated: true`` + ``mimir_id`` so the
  vault collector can skip self-ingesting them (spec §7.2);
- everything under the title is a managed section, so
  ``human_content_hash`` is stable across card updates and a content
  refresh never trips the human-content conflict check (spec §7.4-b).

Cards deliberately carry no volatile frontmatter state (no ``status`` /
``last_verified``); state lives in managed sections only.
"""

from __future__ import annotations

import re


class CardRenderError(ValueError):
    """Raised when a card would violate the naming or section contracts."""


_SECTION_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MIMIR_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$", re.IGNORECASE)


def _escape_yaml(value: str) -> str:
    """Minimal YAML scalar quoting for titles (never quotes we can't own)."""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + value + '"'


def render_card(*, mimir_id: str, title: str, sections: dict[str, str]) -> str:
    """Render an entity card. ``sections`` maps managed-section name → body."""
    if not _MIMIR_ID.match(mimir_id):
        raise CardRenderError(f"mimir_id is not filename-safe: {mimir_id!r}")
    if not sections:
        raise CardRenderError("a card needs at least one managed section")
    for name in sections:
        if not _SECTION_NAME.match(name):
            raise CardRenderError(f"section name must be [a-z][a-z0-9_-]{{0,63}}: {name!r}")

    lines = [
        "---",
        "mimir_generated: true",
        f"mimir_id: {mimir_id}",
        f"title: {_escape_yaml(title)}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    for name, body in sections.items():
        lines.append(f"<!-- mimir:managed:start name={name} -->")
        lines.append(body.rstrip())
        lines.append(f"<!-- mimir:managed:end name={name} -->")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def card_filename(mimir_id: str, title: str) -> str:
    """Stable card filename: the mimir_id only.

    The title is accepted for call-site readability but deliberately unused —
    renaming cards by title would break Obsidian backlinks, and titles may
    carry path-hostile characters (``/`` ``:``).
    """
    if not _MIMIR_ID.match(mimir_id):
        raise CardRenderError(f"mimir_id is not filename-safe: {mimir_id!r}")
    return f"{mimir_id}.md"
