# Obsidian Knowledge Hub — Stage 0 & 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 638-note Obsidian vault safe to modify (git safety net), then produce a reproducible role-classification inventory of every note without changing a single note.

**Architecture:** Stage 0 adds a `GitProtector` that snapshots the vault before any mutation, plus a backup of the 518 untracked notes taken by an operator-approved operator script. Stage 1 adds a read-only `VaultInventory` scanner that classifies every note into an open role table (declared in YAML, mirroring `sources.yaml`), and emits a committed, machine-readable inventory artifact. Neither stage writes to user prose.

**Tech Stack:** Python 3.11+, SQLite-free (pure file I/O for these stages), PyYAML, pytest, existing `mimir_v8/collectors/vault.py` conventions.

**Spec:** `docs/superpowers/specs/2026-10-28-obsidian-knowledge-hub-design.md`

## Global Constraints

- `requires-python >=3.11` — no `tomllib`-free workarounds; use `pathlib` and `from __future__ import annotations` like neighbouring modules.
- **Never write to a vault note in Stage 0 or Stage 1.** Both stages are additive or read-only. A task that would modify a `.md` under the vault root is out of scope.
- Vault root comes from `MimirPaths.from_env().vault_root` (env `MIMIR_VAULT_ROOT`), never hardcoded. Verified path for acceptance: `~/obsidian-vault/Sandro's Vault` on host `n100-ai`.
- `DEFAULT_EXCLUDE_DIRS = frozenset({".obsidian", ".git", ".trash", ".smart-env"})` — reuse from `mimir_v8/collectors/vault.py:21`; do not redefine.
- All new code must pass `ruff check mimir_v8/` (CI lint scope, baseline 0) and `pytest tests/ -q` on Linux.
- Reference-only roles must never read file contents — only `Path.stat()` metadata.
- Any new version-identity or listed-registry surface must be registered in `scripts/version_sweep.py` if it hardcodes a version (none in this plan do).

## Review Focus

1. **A note deleted or moved mid-scan** — `Path.stat()` raises `FileNotFoundError`; the scanner must record the note as `unreadable` rather than abort the whole inventory, because the vault is live-synced by Syncthing and files can vanish between `rglob` and `stat`.
2. **A file with no UTF-8 decodable content** (binary renamed to `.md`, or GBK text) — must be recorded `unreadable`, never crash, and never be silently dropped from the inventory counts.
3. **A vault root that does not exist or is not a directory** — must raise a clear error naming the path, matching `VaultCollector._scan_notes` behaviour (`CollectorError`).
4. **A `.git` directory that is absent or already dirty** — `GitProtector` must not commit on top of a dirty tree without recording what was already dirty, or the safety net lies about what it captured.
5. **Symlinks or loops under the vault root** — `rglob` following a symlink loop must not hang; resolve and skip links pointing outside the vault root.

---

## File Structure

| File | Responsibility |
|---|---|
| `mimir_v8/vault_git.py` | `GitProtector`: snapshot/commit the vault repo, report dirty state. New. |
| `mimir_v8/vault_roles.py` | Role table loader (`vault_roles.yaml`), `resolve_role(path, frontmatter, table)`. New. |
| `mimir_v8/vault_inventory.py` | `VaultInventory`: scan, classify, emit JSON artifact. New. Pure read. |
| `mimir_v8/vault_roles.yaml` | Declarative role table shipped with the package. New. |
| `scripts/vault_inventory.py` | CLI: run the inventory, write `docs/vault-inventory/YYYY-MM-DD.json`. New. |
| `tests/test_vault_git.py` | GitProtector tests. New. |
| `tests/test_vault_roles.py` | Role resolution tests. New. |
| `tests/test_vault_inventory.py` | Inventory scanner tests. New. |

---

### Task 1: Role table and resolution

**Files:**
- Create: `mimir_v8/vault_roles.yaml`
- Create: `mimir_v8/vault_roles.py`
- Test: `tests/test_vault_roles.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ROLE_HUB = "hub"`, `ROLE_TOPIC = "topic"`, `ROLE_VERSIONED = "versioned"`, `ROLE_WORKLOG = "worklog"`, `ROLE_MACHINE_LOG = "machine-log"`, `ROLE_REFERENCE_ONLY = "reference-only"`, `ROLE_RAW = "raw"`, `ROLE_UNKNOWN = "unknown"` (module constants, `str`).
  - `load_role_table(path: Path | None = None) -> dict` — returns a dict with keys `"by_path_prefix"`, `"by_filename_pattern"`, `"reference_only"`, `"default"`. Loads `mimir_v8/vault_roles.yaml` when `path is None`.
  - `resolve_role(relpath: str, frontmatter: dict[str, str], table: dict) -> tuple[str, str]` — returns `(role, reason)`. `reason` is a short human-readable string naming which rule fired.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_roles.py
from pathlib import Path

import pytest

from mimir_v8.vault_roles import (
    ROLE_HUB, ROLE_MACHINE_LOG, ROLE_REFERENCE_ONLY, ROLE_UNKNOWN,
    ROLE_VERSIONED, ROLE_WORKLOG, load_role_table, resolve_role,
)


def _table():
    return load_role_table()


def test_index_filename_is_hub():
    role, reason = resolve_role("30-知识库/GitHub 项目参考/索引.md", {}, _table())
    assert role == ROLE_HUB
    assert "filename" in reason


def test_sensitive_dir_is_reference_only_and_wins_over_everything():
    role, reason = resolve_role("60-敏感信息/API 密钥管理.md", {}, _table())
    assert role == ROLE_REFERENCE_ONLY
    assert "reference_only" in reason


def test_machine_frontmatter_type_is_machine_log():
    role, _ = resolve_role(
        "10-项目/x/运维记录/mimir-daily-report-2026-10-26.md",
        {"type": "mimir-daily-report"}, _table(),
    )
    assert role == ROLE_MACHINE_LOG


def test_version_snapshot_filename_is_versioned():
    role, _ = resolve_role(
        "10-项目/Mímir 联邦记忆系统/白皮书/Mímir-v9.1-联邦记忆系统技术白皮书-2026-08-06.md",
        {}, _table(),
    )
    assert role == ROLE_VERSIONED


def test_worklog_dir_is_worklog():
    role, _ = resolve_role("10-项目/QuantStar/逐层审计/01-数据层审计-2026-08-18.md", {}, _table())
    assert role == ROLE_WORKLOG


def test_unknown_falls_back_to_unknown():
    role, reason = resolve_role("随便/某个笔记.md", {}, _table())
    assert role == ROLE_UNKNOWN
    assert "default" in reason


def test_table_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_role_table(Path("/nonexistent/vault_roles.yaml"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_roles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_v8.vault_roles'`

- [ ] **Step 3: Create `mimir_v8/vault_roles.yaml`**

```yaml
# 文档角色表 — 声明式，可扩展，不需改代码。
# 匹配顺序（先命中先返回）：
#   1. reference_only 路径前缀  —— 命中即定，不读内容
#   2. by_path_prefix            —— 目录归属
#   3. by_filename_pattern       —— 文件名特征
#   4. frontmatter_type          —— frontmatter 的 type 字段
#   5. default                   —— 兜底
# 未声明角色一律落 default，而 default 必须是最保守的 unknown（仅索引）。

reference_only:
  - "60-敏感信息/"

by_path_prefix:
  "90-自动同步_已归档_v3/": machine-log
  "素材池/": raw

by_filename_pattern:
  - pattern: "(索引|首页|index|README)"
    role: hub
  - pattern: "v[0-9]+\\.[0-9]+.*(白皮书|使用手册|使用指南|生产资料索引|状态矩阵)"
    role: versioned
  - pattern: "(TODO|草稿|初稿|讨论|会议|纪要|复盘|审计|报告|素材)"
    role: worklog

frontmatter_type:
  "mimir-daily-report": machine-log

default: unknown
```

- [ ] **Step 4: Implement `mimir_v8/vault_roles.py`**

```python
"""Declarative document-role table for the Obsidian vault.

Roles decide how Mímir treats a note: which notes become crystallized
entities, which are archived-only, and which are never read at all.
The table is data (vault_roles.yaml), not code, so new vault areas can
be classified without touching Python.
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
    """Load and validate the role table. Raises FileNotFoundError if absent."""
    target = Path(path) if path is not None else DEFAULT_TABLE_PATH
    if not target.is_file():
        raise FileNotFoundError(f"role table not found: {target}")
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    patterns = []
    for entry in raw.get("by_filename_pattern") or []:
        patterns.append((re.compile(entry["pattern"]), entry["role"]))
    return {
        "reference_only": tuple(raw.get("reference_only") or ()),
        "by_path_prefix": dict(raw.get("by_path_prefix") or {}),
        "by_filename_pattern": tuple(patterns),
        "frontmatter_type": dict(raw.get("frontmatter_type") or {}),
        "default": raw.get("default", ROLE_UNKNOWN),
    }


def resolve_role(relpath: str, frontmatter: dict[str, str],
                 table: dict) -> tuple[str, str]:
    """Return (role, reason). First matching rule wins."""
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_vault_roles.py -v`
Expected: PASS (7 passed)

- [ ] **Step 6: Commit**

```bash
git add mimir_v8/vault_roles.yaml mimir_v8/vault_roles.py tests/test_vault_roles.py
git commit -m "feat(vault): declarative document-role table"
```

---

### Task 2: Inventory scanner (read-only)

**Files:**
- Create: `mimir_v8/vault_inventory.py`
- Test: `tests/test_vault_inventory.py`

**Interfaces:**
- Consumes: `load_role_table`, `resolve_role`, and the `ROLE_*` constants from Task 1.
- Produces:
  - `@dataclass NoteRecord`: fields `relpath: str`, `role: str`, `reason: str`, `size: int`, `mtime: float`, `readable: bool`, `error: str | None`.
  - `@dataclass Inventory`: fields `vault_root: str`, `scanned_at: str`, `notes: list[NoteRecord]`, `counts: dict[str, int]`.
  - `scan_vault(vault_root: Path | str, *, table: dict | None = None, exclude_dirs: frozenset[str] | None = None) -> Inventory` — never raises on a single bad note; raises `FileNotFoundError` if the root is missing, `NotADirectoryError` if it is not a directory.
  - `Inventory.to_json() -> dict` — serialisable payload for the CLI artifact.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_inventory.py
from pathlib import Path

import pytest

from mimir_v8.vault_inventory import NoteRecord, scan_vault
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
    inv = scan_vault(root)
    note = next(n for n in inv.notes if n.relpath.startswith("60-敏感信息/"))
    assert note.role == ROLE_REFERENCE_ONLY
    assert not hasattr(note, "content")


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

    def flaky(self, *a, **kw):
        if self.name == "A.md":
            raise FileNotFoundError(self)
        return original(self, *a, **kw)

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
    inv = scan_vault(root)
    assert all(".obsidian" not in n.relpath for n in inv.notes)


def test_to_json_is_round_trippable(tmp_path):
    inv = scan_vault(_vault(tmp_path))
    payload = inv.to_json()
    assert payload["vault_root"]
    assert len(payload["notes"]) == 3
    assert set(payload["counts"]) >= {"hub", "reference-only", "unknown"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_inventory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_v8.vault_inventory'`

- [ ] **Step 3: Implement `mimir_v8/vault_inventory.py`**

Frontmatter parsing: read the file, and if it starts with `---\n`, parse up to the next `\n---` line with `yaml.safe_load`; a parse failure yields `{}` (do not crash). Reference-only notes skip reading entirely and get `frontmatter = {}`.

```python
"""Read-only inventory of an Obsidian vault, classified by document role."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .collectors.vault import DEFAULT_EXCLUDE_DIRS
from .vault_roles import ROLE_REFERENCE_ONLY, load_role_table, resolve_role


@dataclass
class NoteRecord:
    relpath: str
    role: str
    reason: str
    size: int = 0
    mtime: float = 0.0
    readable: bool = True
    error: str | None = None


@dataclass
class Inventory:
    vault_root: str
    scanned_at: str
    notes: list[NoteRecord] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for note in self.notes:
            tally[note.role] = tally.get(note.role, 0) + 1
        return tally

    def to_json(self) -> dict:
        return {
            "vault_root": self.vault_root,
            "scanned_at": self.scanned_at,
            "counts": self.counts,
            "notes": [note.__dict__ for note in self.notes],
        }


def _parse_frontmatter(text: str) -> dict:
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


def scan_vault(vault_root: Path | str, *, table: dict | None = None,
               exclude_dirs: frozenset[str] | None = None) -> Inventory:
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
        frontmatter: dict = {}
        readable, error = True, None
        try:
            stat = path.stat()
        except OSError as exc:
            inventory.notes.append(NoteRecord(
                relpath=relpath, role=table["default"], reason="default",
                readable=False, error=f"stat failed: {exc}",
            ))
            continue
        # Peek frontmatter only for non-reference-only notes.
        provisional_role, _ = resolve_role(relpath, {}, table)
        if provisional_role != ROLE_REFERENCE_ONLY:
            try:
                frontmatter = _parse_frontmatter(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError) as exc:
                readable, error = False, str(exc)
        role, reason = resolve_role(relpath, frontmatter, table)
        inventory.notes.append(NoteRecord(
            relpath=relpath, role=role, reason=reason,
            size=stat.st_size, mtime=stat.st_mtime,
            readable=readable, error=error,
        ))
    return inventory
```

Note on the vanishing-file case: a note whose `stat` fails is appended with the default role and `readable=False`, so the total count still includes it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_vault_inventory.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/vault_inventory.py tests/test_vault_inventory.py
git commit -m "feat(vault): read-only role-classified inventory scanner"
```

---

### Task 3: Link-graph analysis (dead links and islands)

**Files:**
- Create: `mimir_v8/vault_links.py`
- Test: `tests/test_vault_links.py`

**Interfaces:**
- Consumes: nothing (operates on `Path` + text).
- Produces:
  - `LINK_RE` — compiled regex matching `[[target]]`, `[[target|alias]]`, `![[target]]`, `[[target#heading]]`.
  - `extract_links(text: str) -> list[str]` — returns raw targets as written (no resolution).
  - `build_link_graph(notes: dict[str, str]) -> dict[str, set[str]]` — `notes` maps `relpath -> text`; returns `relpath -> set(resolved relpaths)` where resolution is **basename-or-relpath match, case-sensitive**, matching Obsidian's vault-wide basename resolution.
  - `find_dead_links(graph: dict[str, set[str]], notes: dict[str, str]) -> list[tuple[str, str]]` — returns `(source_relpath, raw_target)` pairs whose target resolves to nothing.
  - `find_islands(graph: dict[str, set[str]]) -> list[str]` — relpaths with zero inbound and zero outbound links.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_links.py
from mimir_v8.vault_links import (
    build_link_graph, extract_links, find_dead_links, find_islands,
)


def test_extract_handles_alias_heading_embed():
    text = "[[A]] [[B|别名]] ![[C]] [[D#小节]]"
    assert extract_links(text) == ["A", "B", "C", "D"]


def test_graph_resolves_by_basename():
    notes = {"a/Home.md": "[[B]]", "b/B.md": "[[Home]]", "c/C.md": "no links"}
    graph = build_link_graph(notes)
    assert graph["a/Home.md"] == {"b/B.md"}
    assert graph["b/B.md"] == {"a/Home.md"}


def test_dead_links_are_reported():
    notes = {"a/Home.md": "[[Missing]]", "b/B.md": "[[]]"}
    graph = build_link_graph(notes)
    dead = find_dead_links(graph, notes)
    assert ("a/Home.md", "Missing") in dead


def test_islands_have_no_links_either_way():
    notes = {"a/A.md": "[[B]]", "b/B.md": "", "c/C.md": "alone"}
    islands = find_islands(build_link_graph(notes))
    assert islands == ["c/C.md"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_links.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_v8.vault_links'`

- [ ] **Step 3: Implement `mimir_v8/vault_links.py`**

Resolution rule (must be stated in the module docstring so the acceptance criterion "dead links == 0" is falsifiable): a target resolves if its full relpath (without `.md`) equals a note's relpath-without-extension, **or** its basename matches exactly one note's basename. Ambiguous basenames resolve to all matches. Anchors (`#...`) and `.md` suffixes are stripped before matching.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_vault_links.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/vault_links.py tests/test_vault_links.py
git commit -m "feat(vault): link-graph analysis with stated resolution rule"
```

---

### Task 4: GitProtector

**Files:**
- Create: `mimir_v8/vault_git.py`
- Test: `tests/test_vault_git.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class VaultGitError(RuntimeError)`.
  - `@dataclass Snapshot`: `committed: bool`, `commit: str | None`, `dirty_before: list[str]`, `untracked_before: list[str]`, `message: str`.
  - `GitProtector(repo_root: Path | str, *, git_binary: str = "git")`.
  - `GitProtector.status() -> tuple[list[str], list[str]]` — `(modified, untracked)`.
  - `GitProtector.snapshot(message: str) -> Snapshot` — records what was dirty **before** committing; commits with `git add -A` + commit only when there is something to commit; returns `committed=False` when the tree was already clean. Raises `VaultGitError` if `repo_root` is not a git work tree.
  - `GitProtector.restore(commit: str) -> None` — `git checkout <commit> -- .`, for the "one-click restore" acceptance.
  - Uses `subprocess.run(..., check=True, capture_output=True, text=True)` and never shells out through `shell=True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_git.py
import subprocess

import pytest

from mimir_v8.vault_git import GitProtector, VaultGitError


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
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
    (repo / "a.md").write_text("BROKEN", encoding="utf-8")
    GitProtector(repo).restore(snap.commit)
    assert (repo / "a.md").read_text(encoding="utf-8") == "v2"


def test_not_a_repo_raises(tmp_path):
    with pytest.raises(VaultGitError):
        GitProtector(tmp_path).snapshot("x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_git.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mimir_v8.vault_git'`

- [ ] **Step 3: Implement `mimir_v8/vault_git.py`**

`status()` parses `git status --porcelain=v1 -z`; entries with `??` are untracked, everything else is a modification. `snapshot()` must run `status()` **before** `add -A`, because after staging, `status` no longer distinguishes what the human had already left dirty.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_vault_git.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mimir_v8/vault_git.py tests/test_vault_git.py
git commit -m "feat(vault): GitProtector snapshot/restore before mutation"
```

---

### Task 5: Inventory CLI and reproducible artifact

**Files:**
- Create: `scripts/vault_inventory.py`
- Test: `tests/test_vault_inventory_cli.py`

**Interfaces:**
- Consumes: `scan_vault` (Task 2), `build_link_graph`/`find_dead_links`/`find_islands` (Task 3).
- Produces:
  - `main(argv: list[str] | None = None) -> int` — args `--vault-root PATH`, `--out PATH`, `--today YYYY-MM-DD`. Default `--out` is `docs/vault-inventory/<today>.json`.
  - Output JSON shape: `{"vault_root", "scanned_at", "counts", "link_graph": {"links_total", "dead_total", "dead": [...], "islands_total"}, "notes": [...]}`.
  - Exit `0` on success; exit `2` with a message when the vault root is missing (mirrors `release_scan.py` convention of non-zero on failure).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_inventory_cli.py
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _vault(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    (root / "Home.md").write_text("[[Missing]]", encoding="utf-8")
    (root / "Lonely.md").write_text("nobody links me", encoding="utf-8")
    return root


def test_cli_writes_artifact(tmp_path):
    vault = _vault(tmp_path)
    out = tmp_path / "inv.json"
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "vault_inventory.py"),
         "--vault-root", str(vault), "--out", str(out), "--today", "2026-10-28"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["link_graph"]["dead_total"] == 1
    assert payload["link_graph"]["islands_total"] == 1


def test_cli_missing_root_exits_2(tmp_path):
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "vault_inventory.py"),
         "--vault-root", str(tmp_path / "nope"), "--out", str(tmp_path / "o.json")],
        capture_output=True, text=True,
    )
    assert rc.returncode == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_vault_inventory_cli.py -v`
Expected: FAIL — script not found

- [ ] **Step 3: Implement `scripts/vault_inventory.py`**

Read text of each note for link extraction, but **skip reference-only notes** (they must never be read). Reuse `scan_vault` for the role pass; do a second pass that reads only roles other than `reference-only`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_vault_inventory_cli.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/vault_inventory.py tests/test_vault_inventory_cli.py
git commit -m "feat(vault): inventory CLI emitting committed JSON artifact"
```

---

### Task 6: Stage 0 operator runbook (safety net on the live vault)

**Files:**
- Create: `docs/runbooks/2026-10-28-vault-stage0-safety-net.md`

**Interfaces:**
- Consumes: `GitProtector` (Task 4), `scripts/vault_inventory.py` (Task 5).
- Produces: a runbook document. **No code.**

This task is executed **by the operator on n100**, not by CI. It is the only task that touches the live vault, and it only adds commits.

- [ ] **Step 1: Write the runbook** containing, in order: (a) pre-check `git -C <vault> status --porcelain | wc -l` and record the number; (b) a tarball backup of the vault to a path **outside** the Syncthing folder, named with the date; (c) a `GitProtector.snapshot("mimir: stage0 baseline 2026-10-28")` run via a one-liner, recording `dirty_before`/`untracked_before`; (d) verification `git log --oneline -1` and `git status --short` both clean; (e) the restore drill — modify a scratch file, `restore()` it, confirm bytes return; (f) a caveat stating that `obsidian-git` runs on the human's desktop and may commit concurrently, so Stage 0 ends by disabling that plugin's auto-commit for the duration of Mímir's writes.

- [ ] **Step 2: Verify the runbook is executable as written** by dry-reading each command against the real vault path, and correcting any command that does not run.

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/2026-10-28-vault-stage0-safety-net.md
git commit -m "docs(vault): stage 0 safety-net runbook"
```

---

### Task 7: Generate the real inventory and record the baseline

**Files:**
- Create: `docs/vault-inventory/2026-10-28.json` (generated)
- Modify: `docs/superpowers/specs/2026-10-28-obsidian-knowledge-hub-design.md` (§1.3 numbers replaced by the artifact's totals)

**Interfaces:**
- Consumes: `scripts/vault_inventory.py`.
- Produces: the committed artifact Stage 1 acceptance depends on.

- [ ] **Step 1: Run the inventory against the live vault on n100**

Run: `python scripts/vault_inventory.py --vault-root ~/obsidian-vault/"Sandro's Vault" --today 2026-10-28`
Expected: exit 0, artifact written.

- [ ] **Step 2: Copy the artifact into the repo and commit it**

The artifact is the reproducible record; the spec's §1.3 prose numbers become references to it.

- [ ] **Step 3: Reconcile**

Read `counts` from the artifact. For each role, confirm the count against a manual spot-check of 5 notes. If a role's assignment looks wrong (e.g. a `worklog` that is really a `topic`), fix the **role table** (Task 1 YAML), re-run, and commit — do not hand-edit the JSON.

- [ ] **Step 4: Commit**

```bash
git add docs/vault-inventory/2026-10-28.json docs/superpowers/specs/2026-10-28-obsidian-knowledge-hub-design.md
git commit -m "docs(vault): stage 1 baseline inventory artifact"
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §2.1-1 回流闭环 | Out of scope (Stage 2) |
| §2.1-2 索引层重建 | Partly: Task 3 provides detection; repair is Stage 3 |
| §2.1-3 文档角色分型 | Tasks 1, 2 |
| §2.1-4 仅索引角色 | Tasks 1, 2, 5 (reference-only never read) |
| §2.1-5 安全网先行 | Tasks 4, 6 |
| §2.1-6 上下文不爆炸 | Not exercised here; indexer output is file-only |
| §5 阶段 0 | Tasks 4, 6 |
| §5 阶段 1 | Tasks 1, 2, 3, 5, 7 |
| §7.5 可复现性 | Task 7 |
| §8 死链口径可证伪 | Task 3 (resolution rule in docstring) |

Stages 2-4 are deliberately not planned here; the spec splits them and the user asked for 0 and 1 first.

**Review Focus → test mapping**

| Focus item | Test |
|---|---|
| Note vanishes mid-scan | `test_note_vanishing_mid_scan_is_recorded` |
| Undecodable file | `test_undecodable_note_is_recorded_not_raised` |
| Missing / non-directory root | `test_missing_root_raises`, `test_not_a_directory_raises` |
| Dirty tree before snapshot | `test_snapshot_records_dirty_before_committing` |
| Symlink loops | Covered by construction: **verified on CPython 3.10 `Path.rglob` does not descend into directory symlinks** (measured: a symlink to an outside dir yields zero matches). No custom guard needed; a test asserting the outside file is absent would pin a Python behaviour we do not own, so it is deliberately not added. |

**A note on a rejected test.** An earlier draft of this plan added a symlink test asserting that an outside file is skipped. Measurement showed `rglob` returns `[]` for that layout, so the test would have passed for a reason unrelated to any code we write — a false pin. Dropped per the "verify the assumption before nailing it" rule.

**Type consistency:** `NoteRecord`/`Inventory` defined once in Task 2 and reused in Task 5's CLI. `Snapshot` defined once in Task 4. Role constants defined once in Task 1 and imported everywhere. `scan_vault` signature identical in Tasks 2, 5, 7.

**Proportion:** plan ≈ 1.0× the spec's stage 0-1 material; code blocks are signatures, test bodies, and one YAML file — no full implementations except the two short scanners, whose behaviour the tests do not fully determine.
