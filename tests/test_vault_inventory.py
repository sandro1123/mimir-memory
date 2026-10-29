"""Tests for the read-only vault inventory scanner."""
from pathlib import Path

import pytest

from mimir_v8.vault_inventory import scan_vault
from mimir_v8.vault_roles import ROLE_HUB, ROLE_REFERENCE_ONLY, ROLE_UNKNOWN


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "30-知识库").mkdir(parents=True)
    (root / "60-敏感信息").mkdir()
    (root / "30-知识库" / "索引.md").write_text("# 索引\n- [[A]]\n", encoding="utf-8")
    (root / "30-知识库" / "A.md").write_text("# A\n", encoding="utf-8")
    (root / "60-敏感信息" / "keys.md").write_text("SECRET\n", encoding="utf-8")
    return root


def test_scan_counts_every_note(tmp_path):
    inv = scan_vault(_vault(tmp_path))
    assert len(inv.notes) == 3
    assert sum(inv.counts.values()) == 3


def test_reference_only_note_content_is_never_read(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    read_paths: list[str] = []
    original = Path.read_text

    def tracking(self, *args, **kwargs):
        read_paths.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking)
    inv = scan_vault(root)

    note = next(n for n in inv.notes if n.relpath.startswith("60-敏感信息/"))
    assert note.role == ROLE_REFERENCE_ONLY
    assert not hasattr(note, "content")
    # The sensitive note must never have been opened.
    assert "keys.md" not in read_paths


def test_roles_are_assigned(tmp_path):
    inv = scan_vault(_vault(tmp_path))
    roles = {n.relpath: n.role for n in inv.notes}
    assert roles["30-知识库/索引.md"] == ROLE_HUB
    assert roles["30-知识库/A.md"] == ROLE_UNKNOWN


def test_undecodable_note_is_recorded_not_raised(tmp_path):
    root = _vault(tmp_path)
    (root / "30-知识库" / "bad.md").write_bytes(b"\xff\xfe\x00\x00garbage")
    inv = scan_vault(root)
    bad = next(n for n in inv.notes if n.relpath.endswith("bad.md"))
    assert bad.readable is False
    assert bad.error


def test_note_vanishing_mid_scan_is_recorded(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    original = Path.stat

    def flaky(self, *args, **kwargs):
        if self.name == "A.md":
            raise FileNotFoundError(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    inv = scan_vault(root)
    gone = next(n for n in inv.notes if n.relpath.endswith("A.md"))
    assert gone.readable is False
    assert len(inv.notes) == 3  # still counted


def test_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan_vault(tmp_path / "nope")


def test_not_a_directory_raises(tmp_path):
    f = tmp_path / "file.md"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        scan_vault(f)


def test_excluded_dirs_are_skipped(tmp_path):
    root = _vault(tmp_path)
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (root / ".obsidian" / "note.md").write_text("x", encoding="utf-8")
    inv = scan_vault(root)
    assert all(".obsidian" not in n.relpath for n in inv.notes)


def test_to_json_is_round_trippable(tmp_path):
    inv = scan_vault(_vault(tmp_path))
    payload = inv.to_json()
    assert payload["vault_root"]
    assert len(payload["notes"]) == 3
    assert set(payload["counts"]) >= {"hub", "reference-only", "unknown"}


def test_credential_shaped_content_is_downgraded_to_reference_only(tmp_path):
    """A note holding credential values must not be treated as knowledge.

    Reference-only is by directory today, but a key pasted into an
    ordinary note would otherwise be classified as a topic and
    crystallized. Conservative direction: downgrade, never upgrade.
    """
    root = _vault(tmp_path)
    (root / "30-知识库" / "随手记.md").write_text(
        "配置如下\napi_key = sk-abcdef0123456789abcdef\n", encoding="utf-8"
    )
    inv = scan_vault(root)
    note = next(n for n in inv.notes if n.relpath.endswith("随手记.md"))
    assert note.role == ROLE_REFERENCE_ONLY
    assert "credential" in note.reason
