# 阶段 0 操作 runbook：Obsidian 知识中枢安全网建设

> **文档日期**：2026-10-30
> **状态**：待用户确认后执行
> **目标版本**：Mímir 1.7.0（阶段 0，前置，必须先做）
> **关联设计稿**：`docs/superpowers/specs/2026-10-28-obsidian-knowledge-hub-design.md` §5 阶段 0
> **执行机**：n100 (`ssh <user>@<n100-lan-ip>`)，库路径 `$HOME/obsidian-vault/Sandro's Vault`
> **核心原则**：**只增不改，绝不改动任何 note 内容**。任何写入/整理操作前必须先有 git 快照。

---

## 1. 目的与范围

把库里 **518 篇（实测以 `git status` 为准，随 Syncthing 同步波动）从未提交的未跟踪 .md 笔记**纳入 git 快照，并落地 `GitProtector`（写入前自动 git 快照，可一键还原）。

这是 1.7.0 双向反哺的前置安全网：阶段 1（清册、只读）、阶段 2（回流、只写新文件）、阶段 3（修死链、动既有文件）都以此为基线。阶段 0 之后，对任一文件的改动都可从快照一键还原。

**非目标**：不改任何 note 的正文/frontmatter；不处理已修改文件的内容冲突；不推远程（库纯本地 commit，经 Syncthing 离机）。

---

## 2. n100 现状（2026-10-30 预检实测）

| 检查项 | 实测结果 | 说明 |
|---|---|---|
| HEAD | `2adec49 docs: 记录群内多Agent交流协议研读与落地指引笔记` | 当前基线 |
| remote | （空） | 纯本地 commit，无远程推送 |
| `git status --short` 总数 | 149 条（预检时刻） | 随 Syncthing 同步波动；设计稿 §1.3 10-29 实测为 518 篇未跟踪，以制品为准 |
| 已修改文件 | **1 条**：`60-敏感信息/API 密钥管理.md` | ⚠️ 敏感，**不纳入本次 commit**，由用户单独决策 |
| 未跟踪文件 | 多目录（`004-*/`、`005-*/`、`10-Projects/`、`10-项目/ATLAS …`） | 需逐条确认只为 `.md` 笔记 |
| Syncthing | 运行中（2 个进程，PID 1975/2015） | 库正在同步，操作时避免与 Syncthing 写入冲突 |
| obsidian-git 插件 | 已安装（`main.js`/`manifest.json`/`obsidian_askpass.sh`） | 与本次 git 操作并存，不冲突 |

> 预检命令（已在本轮执行，只读，无副作用）：
> ```bash
> ssh <user>@<n100-lan-ip> 'cd "$HOME/obsidian-vault/Sandro'\''s Vault" && git status --short | head -5; git status --short | wc -l; git remote -v; git log --oneline -1'
> ```

---

## 3. 核心约束（红线，违者中止）

1. **严禁在安全网建成前动库**：本次只做 `git add`（新文件）+ `git commit`，**不修改任何已存在文件内容**。
2. **严禁把 `60-敏感信息/` 的已修改文件 `API 密钥管理.md` 混入本次 commit**。该文件由用户单独决策是否纳入、何时纳入。
3. **严禁推远程**：commit 后留在本地，经 Syncthing 自然离机。
4. **严禁在 Syncthing 活跃写入时批量 commit**：若 Syncthing 正在同步大量文件，先暂停或等待空窗，避免 `git add` 与文件系统变更竞争。
5. 任何写入/整理操作（阶段 2/3）前必须先通过 `GitProtector` 做 git 快照。

---

## 4. 执行步骤（四步）

### 步骤 1：只读预检（已完成，可重跑）

在 n100 库目录下执行：

```bash
cd "$HOME/obsidian-vault/Sandro's Vault"
echo "=== 已修改/未跟踪条目 ==="
git status --short
echo "=== 总数 ==="
git status --short | wc -l
echo "=== remote ==="
git remote -v
echo "=== HEAD ==="
git log --oneline -1
```

确认：
- HEAD 为 `2adec49`（或用户指定的基线）；
- 远程为空；
- 已修改文件中只有那 1 个敏感文件（若有其他已修改文件，先查明再决定是否纳入）。

### 步骤 2：生成角色清册（只读，不改库，产出验收制品）

```bash
cd "$HOME/obsidian-vault/Sandro's Vault"
python scripts/vault_inventory.py \
  --vault-root . \
  --out docs/vault-inventory/2026-10-30-stage0-catalog.json \
  --today 2026-10-30
```

该脚本只读，不写库；输出 `docs/vault-inventory/2026-10-30-stage0-catalog.json`，包含 638 篇（实测数）的角色分布与未跟踪清单，作为阶段 1 清册的基线制品（committed，可重跑）。

### 步骤 3：只增不改，将未跟踪 .md 笔记加入 git（需要用户确认）

先列出待加入的未跟踪 .md（只读，审查数量）：

```bash
cd "$HOME/obsidian-vault/Sandro's Vault"
git ls-files --others --exclude-standard | grep '\.md$' | sort > /tmp/stage0-untracked-md.txt
wc -l /tmp/stage0-untracked-md.txt   # 审查：应 ≈ 518，以实际为准
head -20 /tmp/stage0-untracked-md.txt
```

**关键：不得把 `60-敏感信息/API 密钥管理.md` 加入此次 commit。** 若该文件出现在未跟踪/已修改列表中，先从列表中剔除（或由用户决定单独处理）。

确认列表无误后，仅 add 未跟踪的 .md 笔记（`xargs`/`git add` 不改文件内容，只改索引）：

```bash
cd "$HOME/obsidian-vault/Sandro's Vault"
xargs -r git add -- < /tmp/stage0-untracked-md.txt
git status --short   # 应只显示新增的未跟踪→暂存，不应出现已修改的敏感文件
git commit -m @'
stage0: ingest untracked vault notes into git (additive only)

Add 518 previously untracked .md notes to the git snapshot as the
safety net for the Obsidian knowledge-hub bidirectional flow.
No note content is modified; the one already-modified sensitive file
under 60-敏感信息/ is deliberately excluded and left for a separate
decision.

Co-Authored-By: Claude Code <noreply@anthropic.com>
'@
```

> `git add` 只把文件加入暂存区，**不改动任何字节内容**。commit 记录的是首次快照，之后任何改动均可 `git checkout -- <file>` 回到此基线。

### 步骤 4：验收（只读）

```bash
cd "$HOME/obsidian-vault/Sandro's Vault"
echo "=== 工作区应干净（只留已排除的敏感文件若未纳入）==="
git status --short
echo "=== 跟踪的 .md 总数（应较前增加）==="
git ls-files | grep '\.md$' | wc -l
echo "=== HEAD 应为本次 stage0 commit ==="
git log --oneline -1
echo "=== 还原自检：对任一新跟踪文件，git checkout 应无差异 ==="
git diff --quiet HEAD -- <任一新跟踪的 .md 文件> && echo "还原自检通过"
```

验收通过标准：
- `git status --short` 干净，或仅剩那 1 个已排除的敏感文件；
- 跟踪的 `.md` 总数较预检增加了未跟踪笔记数；
- `HEAD` 为本次 stage0 commit；
- 任一新跟踪文件 `git checkout --` 后与 HEAD 无差异（证明只增不改，可一键还原）。

---

## 5. 中止条件

出现以下任一情况，立即停止，不 commit，等待用户决策：

- `git status --short` 中出现除 `60-敏感信息/API 密钥管理.md` 之外的**已修改**文件（说明有人先动了库，需先查明）；
- Syncthing 正在大量写入（`ps aux | grep syncthing` 可见高 IO / 大量临时文件）；
- 未跟踪 `.md` 数量与预期（≈518）偏差过大，或列表中包含非笔记文件（如 `.obsidian/` 下的 `.md`）；
- 用户未对“排除敏感文件 API 密钥管理.md”给出明确确认。

---

## 6. GitProtector 落地标准（阶段 0 产出之一）

设计稿 §4.2 要求 `GitProtector`：写入前自动 git 快照，可一键还原。阶段 0 不实现完整集成，但需**定义接口契约并给出最小实现桩**，供阶段 2/3 调用。

建议新建文件：`mimir_v8/git_protector.py`

```python
"""GitProtector: write-before git snapshot + one-shot restore for the vault.

Stage 0 deliverable: a thin guard so any Mimir->vault write (ObsidianPublisher,
VaultIndexer repair, ...) is preceded by a git commit snapshot, and any written
file can be restored to the last committed state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitProtector:
    def __init__(self, vault_root: Path | str) -> None:
        self.vault_root = Path(vault_root)

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.vault_root,
            capture_output=True,
            text=True,
            check=True,
        )

    def snapshot_before_write(self, paths: list[Path | str], message: str | None = None) -> str:
        """Commit the given paths as a pre-write snapshot. Returns the new HEAD sha."""
        rel = [str(p.relative_to(self.vault_root)) for p in paths]
        self._git("add", "--", *rel)
        msg = message or f"git-protector: pre-write snapshot for {len(rel)} path(s)"
        self._git("commit", "-m", msg)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def restore(self, path: Path | str) -> None:
        """Restore one file to its last committed state (one-shot rollback)."""
        rel = str(Path(path).relative_to(self.vault_root))
        self._git("checkout", "--", rel)

    def is_clean(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=self.vault_root,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and not result.stdout.strip()
```

**集成契约**：
- `ObsidianPublisher` 写入任何卡片前，先对目标文件调用 `snapshot_before_write([target_path])`；
- `VaultIndexer` 阶段 3 修复既有文件前，先对受影响文件快照；
- 还原：`git_protector.restore(path)` 回到上一个 commit（或人工 `git checkout -- <path>`）。

**验收**：在本地仓库上跑一次 `snapshot_before_write` + `restore` 往返，文件字节与快照一致。

---

## 7. 回滚 / 还原说明

- 本次 commit 只做了 `git add`（新文件），未改任何内容。若用户对某篇笔记的加入有异议，`git rm --cached <file>` 即可移出跟踪（不删磁盘文件）；若内容已被错误改动（本次不应发生），`git checkout -- <file>` 回到本次 commit。
- 由于库纯本地 commit、无 remote，回滚只依赖本地 `.git/`，不涉及远程分支。
- Syncthing 会把新 commit 的文件自然同步到其他节点，无需额外操作。

---

## 8. 执行前需要用户确认

在执行步骤 3（`git add` + `commit`）之前，请确认：

1. ✅ 接受以 **2026-10-30 预检结果** 为基线（HEAD `2adec49`，无 remote）；
2. ✅ 接受 **只增不改、绝不改动 note 内容**；
3. ✅ 接受 **把未跟踪 `.md` 笔记（约 518 篇，以 `git ls-files` 实测为准）加入本次 commit**；
4. ✅ 接受 **`60-敏感信息/API 密钥管理.md` 不纳入本次 commit**，由您单独决定后续处置；
5. ✅ 接受 commit 留在 **本地**（无 remote），经 Syncthing 离机。

确认后我将在 n100 执行步骤 3~4，并提交 stage0 commit 与 GitProtector 桩代码到 mimir 本地仓库。
