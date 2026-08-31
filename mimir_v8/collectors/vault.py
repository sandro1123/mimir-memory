"""Vault (Obsidian) collector for Mímir v12.1.0 all-source ingestion.

Scans a markdown vault for notes and converts each into a CollectResult
ready for ingestion into the learning pipeline. Mirrors the RSS collector
contract: ``collect() -> list[CollectResult]`` where ``source_id`` is the
note path relative to the vault root.

Hidden directories (``.obsidian``, ``.git`` …) and template directories
are excluded from scanning.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .base import BaseCollector, CollectResult, CollectorError

TZ = timezone(timedelta(hours=8))

#: Directories never scanned, at any depth.
DEFAULT_EXCLUDE_DIRS = frozenset({".obsidian", ".git", ".trash", ".smart-env"})

#: Max bytes read per note — guard against pathological files.
MAX_NOTE_BYTES = 512_000


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
                content = note.read_text(encoding="utf-8", errors="replace")[
                    : MAX_NOTE_BYTES // 4  # chars ≈ bytes guard
                ]
            except OSError as e:
                raise CollectorError(f"cannot read note {note}: {e}") from e
            rel = note.relative_to(self.vault_root).as_posix()
            mtime = datetime.fromtimestamp(note.stat().st_mtime, tz=TZ).isoformat()
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
