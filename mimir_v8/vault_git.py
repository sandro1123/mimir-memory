"""GitProtector — snapshot and restore a vault working tree.

Stage 0 of the Obsidian knowledge-hub work: the vault had 518 of 638
notes untracked, so ``git checkout`` could not rescue most of it. This
module makes the repo a real safety net before any stage touches a note.

The snapshot deliberately records what was ALREADY dirty before it
commits, because after ``git add -A`` that information is gone — and a
safety net that cannot say what it captured is not a safety net.

Never uses ``shell=True``. Raises ``VaultGitError`` when the target is
not a git work tree, rather than silently doing nothing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class VaultGitError(RuntimeError):
    """Raised when the vault git repository cannot be used."""


@dataclass
class Snapshot:
    """What a ``snapshot()`` call saw and did."""

    committed: bool
    commit: str | None = None
    dirty_before: list[str] = field(default_factory=list)
    untracked_before: list[str] = field(default_factory=list)
    message: str = ""


class GitProtector:
    """Snapshot/restore helper around a git working tree."""

    def __init__(self, repo_root: Path | str, *, git_binary: str = "git"):
        self.repo_root = Path(repo_root)
        self.git_binary = git_binary

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [self.git_binary, *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise VaultGitError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result

    def _require_repo(self) -> None:
        probe = self._run("rev-parse", "--is-inside-work-tree", check=False)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            raise VaultGitError(f"not a git work tree: {self.repo_root}")

    def status(self) -> tuple[list[str], list[str]]:
        """Return ``(modified, untracked)`` for the working tree."""
        self._require_repo()
        raw = self._run("status", "--porcelain=v1", "-z").stdout
        modified: list[str] = []
        untracked: list[str] = []
        for entry in raw.split("\0"):
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if code.strip() == "??":
                untracked.append(path)
            else:
                modified.append(path)
        return sorted(modified), sorted(untracked)

    def snapshot(self, message: str) -> Snapshot:
        """Record the current dirty state, then commit it.

        Returns ``committed=False`` when the tree was already clean.
        """
        dirty, untracked = self.status()
        if not dirty and not untracked:
            return Snapshot(
                committed=False,
                dirty_before=[],
                untracked_before=[],
                message=message,
            )

        self._run("add", "-A")
        self._run("commit", "-q", "-m", message)
        head = self._run("rev-parse", "HEAD").stdout.strip()
        return Snapshot(
            committed=True,
            commit=head,
            dirty_before=dirty,
            untracked_before=untracked,
            message=message,
        )

    def restore(self, commit: str) -> None:
        """Restore every tracked path to its state at ``commit``."""
        self._require_repo()
        self._run("checkout", commit, "--", ".")
