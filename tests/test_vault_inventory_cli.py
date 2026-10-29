"""Tests for the vault inventory CLI."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "vault_inventory.py"


def _vault(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    (root / "Home.md").write_text("[[Missing]]", encoding="utf-8")
    (root / "Lonely.md").write_text("nobody links me", encoding="utf-8")
    return root


def test_cli_writes_artifact(tmp_path):
    vault = _vault(tmp_path)
    out = tmp_path / "inv.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--vault-root", str(vault),
         "--out", str(out), "--today", "2026-10-28"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["link_graph"]["dead_total"] == 1
    # Home.md's only link is dead, so it resolves to nothing: it has no
    # outbound edge and no inbound edge, making it an island too.
    assert payload["link_graph"]["islands_total"] == 2
    assert len(payload["notes"]) == 2


def test_cli_missing_root_exits_2(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--vault-root", str(tmp_path / "nope"),
         "--out", str(tmp_path / "o.json")],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "not found" in result.stderr.lower()


def test_cli_never_reads_reference_only_notes(tmp_path):
    vault = tmp_path / "v"
    (vault / "60-敏感信息").mkdir(parents=True)
    secret = vault / "60-敏感信息" / "keys.md"
    # Undecodable bytes: if the scanner tried to read it, it would be
    # reported unreadable. A reference-only note must look clean.
    secret.write_bytes(b"\xff\xfeSECRET")
    out = tmp_path / "inv.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--vault-root", str(vault),
         "--out", str(out), "--today", "2026-10-28"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    note = payload["notes"][0]
    assert note["role"] == "reference-only"
    assert note["readable"] is True
