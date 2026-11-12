"""Vault (Obsidian) collector for Mímir v12.1.0 all-source ingestion.

Scans a markdown vault for notes and converts each into a CollectResult
ready for ingestion into the learning pipeline. Mirrors the RSS collector
contract: ``collect() -> list[CollectResult]`` where ``source_id`` is the
note path relative to the vault root.

Hidden directories (``.obsidian``, ``.git`` …) and template directories
are excluded from scanning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import BaseCollector, CollectResult, CollectorError


#: Directories never scanned, at any depth.
DEFAULT_EXCLUDE_DIRS = frozenset({".obsidian", ".git", ".trash", ".smart-env"})

#: Max bytes read per note — guard against pathological files.
MAX_NOTE_BYTES = 512_000

#: Frontmatter head size consulted by the self-ingest guard. The marker must
#: sit in the YAML header; a guard reading post-truncation content could miss
#: it, so the head is read separately and BEFORE the body.
_FRONTMATTER_HEAD_BYTES = 4096

_GENERATED_LINE = re.compile(r"^mimir_generated:\s*\"?true\"?\s*$", re.MULTILINE)


def _is_mimir_generated(text: str) -> bool:
    """True when the note's YAML frontmatter carries the Mímir-generated marker.

    Only the frontmatter block (first ``---`` to the second ``---``) is
    consulted; body text claiming to be generated is not trusted, and
    ``mimir_generated: false`` is not a marker.
    """
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    return bool(_GENERATED_LINE.search(text[:end]))


class VaultCollector(BaseCollector):
    """Collect markdown notes from an Obsidian-style vault."""

    def __init__(
        self,
        vault_root: Path | str | None = None,
        exclude_dirs: set[str] | None = None,
        enabled: bool = True,
    ):
        from ..config import MimirPaths

        if vault_root is None:
            vault_root = MimirPaths.from_env().vault_root
        self.vault_root = Path(vault_root)
        self.exclude_dirs = frozenset(exclude_dirs or DEFAULT_EXCLUDE_DIRS)
        super().__init__(name="vault", enabled=enabled)

    def _scan_notes(self) -> list[Path]:
        """Yield markdown files, excluding hidden/template directories."""
        if not self.vault_root.exists():
            raise CollectorError(f"vault root not found: {self.vault_root}")
        notes: list[Path] = []
        for path in sorted(self.vault_root.rglob("*.md")):
            rel_parts = path.relative_to(self.vault_root).parts
            # exclude any file living under an excluded directory name
            if any(part in self.exclude_dirs for part in rel_parts[:-1]):
                continue
            # a note literally named "template.md" is a template file, skip
            if path.name.lower() == "template.md":
                continue
            notes.append(path)
        return notes

    def collect(self) -> list[CollectResult]:
        results: list[CollectResult] = []
        for note in self._scan_notes():
            try:
                head = note.read_text(encoding="utf-8", errors="replace")[
                    : _FRONTMATTER_HEAD_BYTES
                ]
            except OSError as e:
                raise CollectorError(f"cannot read note {note}: {e}") from e
            if _is_mimir_generated(head):
                # Self-ingest guard (§7.2): Mímir-generated cards are never
                # fed back through the learning pipeline, or the bridge
                # loops. They are not even registered as skipped items —
                # from the collector's perspective they are not sources.
                continue
            try:
                content = note.read_text(encoding="utf-8", errors="replace")[
                    : MAX_NOTE_BYTES // 4  # chars ≈ bytes guard
                ]
            except OSError as e:
                raise CollectorError(f"cannot read note {note}: {e}") from e
            rel = note.relative_to(self.vault_root).as_posix()
            results.append(
                CollectResult(
                    source_id=rel,
                    title=note.stem,
                    url="",
                    content=content,
                    items_collected=1,
                    items_skipped=0,
                )
            )
        return results

    def describe(self) -> dict[str, Any]:
        return {
            "type": "vault",
            "vault_root": str(self.vault_root),
            "exclude_dirs": sorted(self.exclude_dirs),
            "enabled": self.enabled,
        }

    def idempotency_key(self, source_id: str) -> str:
        """Stable per-note key that rotates when the note is edited.

        mtime in the key means an edited note ingests again as a new
        version, while an untouched note is deduplicated away.
        """
        note = self.vault_root / source_id
        mtime = int(note.stat().st_mtime)
        return f"vault:{source_id}:{mtime}"
