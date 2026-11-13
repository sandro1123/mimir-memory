#!/usr/bin/env python3
"""prod_drift — 发布目录 vs git 参照的漂移闸门。

判例背景（2026-11-08 审计 / v1.6.0 部署）：
- 生产目录里躺着从未入库的热补丁（store.py BEGIN IMMEDIATE 重试）。
  按 GitHub master 重新部署会悄悄丢失它。
- 生产发布 commit（b829842）从未推上任何远端，tag v1.6.0 也不存在
  ——发布物「不可寻址」本身就是病。本脚本部署前后各跑一次：
  默认参照发布目录所属 git 仓的 HEAD，工作区 vs HEAD = 直改漂移。

支持两种布局：release 目录自身是 git 仓，或它位于某个 git 仓内（如
~/.hermes/mimir/releases/1.x.y 是 ~/.hermes/mimir 仓的子目录）。

用法：
    python3 scripts/prod_drift.py --release-dir <dir>          # 工作区 vs 自身 HEAD
    python3 scripts/prod_drift.py --release-dir <dir> --ref <commit|tag>
    python3 scripts/prod_drift.py ... --allow mimir_v8/store.py
    python3 scripts/prod_drift.py ... --audit-only             # 只列不拦

退出码：0=干净或全部豁免；1=存在未豁免漂移；2=用法/环境错误。
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=check)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release-dir", required=True, type=Path)
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--repo", type=Path, default=None,
                    help="显式参照仓（如 dev 仓）。给定后 release 目录文件逐个与 repo@ref 对比。")
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--audit-only", action="store_true")
    args = ap.parse_args()

    rel: Path = args.release_dir.resolve()

    repo = args.repo.resolve() if args.repo else None

    r = _git(rel, "rev-parse", "--show-toplevel", check=False)
    if r.returncode and repo is None:
        print(f"ERROR: {rel} 不在任何 git 仓内", file=sys.stderr)
        return 2
    root = Path(r.stdout.strip()) if not r.returncode else None
    prefix = ""
    if root is not None:
        prefix = rel.relative_to(root).as_posix()
        if prefix and prefix != ".":
            prefix += "/"

    if repo is not None:
        root = repo
        prefix = ""

    r = _git(root, "rev-parse", "--verify", args.ref, check=False)
    if r.returncode:
        print(f"ERROR: 参照不存在: {args.ref}", file=sys.stderr)
        return 2

    ls = _git(root, "ls-files", "--", prefix or ".").stdout.splitlines()
    rels = [Path(p).relative_to(prefix) for p in ls] if prefix else [Path(p) for p in ls]

    if not rels:
        print(f"FAIL-CLOSED: 参照集为空 —— {rel} 下的文件全部不被 git 跟踪"
              f"（root={root}）。对照 {args.ref} 得到的 CLEAN 是假绿。"
              "请改用 --repo <dev仓> --ref <commit> 显式对比，或把发布物纳入跟踪。",
              file=sys.stderr)
        return 2

    drift: list[tuple[str, str]] = []
    for rp in rels:
        f = rel / rp
        if not f.is_file():
            drift.append((str(rp), "missing-in-worktree"))
            continue
        key = f"{args.ref}:{(prefix + rp.as_posix()) if prefix else rp.as_posix()}"
        r2 = subprocess.run(["git", "-C", str(root), "show", key],
                            capture_output=True)
        if r2.returncode:
            drift.append((str(rp), "not-in-ref"))
        elif _sha(f.read_bytes()) != _sha(r2.stdout):
            drift.append((str(rp), "differs"))

    if not drift:
        print(f"CLEAN: {rel} == {args.ref} (root={root}, prefix={prefix or chr(47)}), no drift.")
        return 0

    print(f"PROD DRIFT: {args.ref} vs {rel} (root={root})")
    allows = set(args.allow)
    blocked = 0
    for path, kind in drift:
        mark = "ALLOWED" if path in allows else "BLOCKED"
        print(f"  [{mark}] {kind:>18}  {path}")
        if mark == "BLOCKED":
            blocked += 1
    print(f"\n总结: {len(drift)} 差异, {blocked} 未豁免")
    if blocked and not args.audit_only:
        print("拒绝部署。先把差异带回仓库，或 --allow <path> 显式豁免。")
        return 1
    if blocked:
        print("审计模式：不拦截。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
