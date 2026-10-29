"""Read-only inventory of an Obsidian vault, classified by document role.

This module never writes to the vault. Its only outputs are the returned
``Inventory`` object and whatever the caller does with it.

Failure policy: a single unreadable note must never abort the scan. Notes
that vanish mid-scan (the vault is live-synced) or that fail to decode are
recorded with ``readable=False`` and still counted, so the totals stay
honest.

Reference-only notes (``60-敏感信息/``) are never opened: only ``stat()``
metadata is read. See ``vault_roles.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .collectors.vault import DEFAULT_EXCLUDE_DIRS
from .vault_roles import ROLE_REFERENCE_ONLY, load_role_table, resolve_role


@dataclass
class NoteRecord:
    """One vault note as seen by the inventory."""

    relpath: str
    role: str
    reason: str
    size: int = 0
    mtime: float = 0.0
    readable: bool = True
    error: str | None = None


@dataclass
class Inventory:
    """The result of a vault scan."""

    vault_root: str
    scanned_at: str
    notes: list[NoteRecord] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Number of notes per role."""
        tally: dict[str, int] = {}
        for note in self.notes:
            tally[note.role] = tally.get(note.role, 0) + 1
        return tally

    def to_json(self) -> dict:
        """Serialisable payload for the CLI artifact."""
        return {
            "vault_root": self.vault_root,
            "scanned_at": self.scanned_at,
            "counts": self.counts,
            "notes": [note.__dict__ for note in self.notes],
        }


def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter, returning {} on any malformed input."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        parsed = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def scan_vault(
    vault_root: Path | str,
    *,
    table: dict | None = None,
    exclude_dirs: frozenset[str] | None = None,
) -> Inventory:
    """Scan ``vault_root`` and classify every ``.md`` note by role.

    Raises:
        FileNotFoundError: the root does not exist.
        NotADirectoryError: the root exists but is not a directory.
    """
    root = Path(vault_root)
    if not root.exists():
        raise FileNotFoundError(f"vault root not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"vault root is not a directory: {root}")

    table = table if table is not None else load_role_table()
    excluded = exclude_dirs if exclude_dirs is not None else DEFAULT_EXCLUDE_DIRS

    inventory = Inventory(
        vault_root=str(root),
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in excluded for part in rel.parts[:-1]):
            continue
        relpath = rel.as_posix()

        try:
            stat = path.stat()
        except OSError as exc:
            # The note vanished between rglob() and stat(). Record it so the
            # totals stay honest rather than silently dropping it.
            inventory.notes.append(
                NoteRecord(
                    relpath=relpath,
                    role=table["default"],
                    reason="default",
                    readable=False,
                    error=f"stat failed: {exc}",
                )
            )
            continue

        # Decide reference-only before opening anything: those notes must
        # never be read, not even to peek at frontmatter.
        provisional_role, _ = resolve_role(relpath, {}, table)
        frontmatter: dict = {}
        readable, error = True, None
        if provisional_role != ROLE_REFERENCE_ONLY:
            try:
                frontmatter = _parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                readable, error = False, str(exc)

        role, reason = resolve_role(relpath, frontmatter, table)
        inventory.notes.append(
            NoteRecord(
                relpath=relpath,
                role=role,
                reason=reason,
                size=stat.st_size,
                mtime=stat.st_mtime,
                readable=readable,
                error=error,
            )
        )

    return inventory
