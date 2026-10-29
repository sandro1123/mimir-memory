"""Tests for GitProtector, the vault snapshot safety net."""
import subprocess

import pytest

from mimir_v8.vault_git import GitProtector, VaultGitError


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.md").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_snapshot_records_dirty_before_committing(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.md").write_text("v2", encoding="utf-8")
    (repo / "new.md").write_text("n", encoding="utf-8")
    snap = GitProtector(repo).snapshot("pre-change")
    assert snap.committed is True
    assert snap.dirty_before == ["a.md"]
    assert snap.untracked_before == ["new.md"]


def test_snapshot_on_clean_tree_does_not_commit(tmp_path):
    repo = _repo(tmp_path)
    snap = GitProtector(repo).snapshot("noop")
    assert snap.committed is False
    assert snap.commit is None


def test_restore_brings_back_previous_bytes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.md").write_text("v2", encoding="utf-8")
    snap = GitProtector(repo).snapshot("before")
    assert snap.commit
    (repo / "a.md").write_text("BROKEN", encoding="utf-8")
    GitProtector(repo).restore(snap.commit)
    assert (repo / "a.md").read_text(encoding="utf-8") == "v2"


def test_not_a_repo_raises(tmp_path):
    with pytest.raises(VaultGitError):
        GitProtector(tmp_path).snapshot("x")


def test_status_reports_modified_and_untracked(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.md").write_text("changed", encoding="utf-8")
    (repo / "b.md").write_text("new", encoding="utf-8")
    modified, untracked = GitProtector(repo).status()
    assert modified == ["a.md"]
    assert untracked == ["b.md"]


def test_snapshot_message_is_recorded(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.md").write_text("v2", encoding="utf-8")
    GitProtector(repo).snapshot("mimir: baseline")
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log == "mimir: baseline"
