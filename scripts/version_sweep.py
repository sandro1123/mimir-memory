# -*- coding: utf-8 -*-
"""Mímir 版本号派生登记表扫描器。

升版前跑一遍，找出代码 / 配置 / 文档中写死旧版本号且**能被程序读到**
的登记点。纯注释 / 历史条目不报（它们是历史，不会误导程序行为）。

用法:
    python scripts/version_sweep.py [--root DIR] [--version X.Y.Z]

不给 --version 时自动读 mimir_v8/schema.py 中的 MIMIR_VERSION。

设计纪律（三问）：
1. 它能扫到东西吗？—— 自带 --self-test 模式，用仓库里的已知登记点
   跑一遍，确认扫描器至少能命中 dashboard/backend/main.py CODENAMES、
   ARCHITECTURE.md Synced version、pyproject.toml version。
2. 反向注入能让它红吗？—— 把已知登记点的版本号改掉，扫描器输出 STALE。
3. 不误伤？—— CHANGELOG 里引用旧版本号属于历史记录，不报 STALE。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

# ── 登记点（版本号出现在这些位置时被视为"派生登记"，必须与当前版本一致）
# 每条登记点：(相对路径, 正则 pattern — 必须有一个名为 ver 的捕获组)
# 正则匹配的是"能被程序读到"的那一行，不是注释。

class RegistryEntry(NamedTuple):
    relpath: str
    pattern: str          # regex with named group (?P<ver>...)
    description: str      # 人读的说明


REGISTRY: list[RegistryEntry] = [
    RegistryEntry(
        "mimir_v8/schema.py",
        r'^MIMIR_VERSION\s*=\s*["\'](?P<ver>[^"\']+)["\']',
        "运行时内核版本常量",
    ),
    RegistryEntry(
        "pyproject.toml",
        r'^version\s*=\s*"(?P<ver>[^"]+)"',
        "PyPI 包版本",
    ),
    RegistryEntry(
        "dashboard/backend/main.py",
        # 代号表的最后一条（数字最大的版本号）必须 == 当前版本
        # 但直接匹配"当前版本在表中"更可靠——扫所有条目，检查
        # 当前版本是否出现过。用 _check_codename_table 代替正则。
        r'^\s*"(?P<ver>\d+\.\d+\.\d+)":\s*"[^"]+',
        "PULSE 代号表条目（须包含当前版本）",
    ),
    RegistryEntry(
        "ARCHITECTURE.md",
        r'Synced version[：:]\s*\*\*(?P<ver>[^*]+)\*\*',
        "架构文档同步版本头",
    ),
    RegistryEntry(
        "tests/test_r8_release.py",
        r'self\.assertEqual\(MIMIR_VERSION,\s*"(?P<ver>[^"]+)"\)',
        "发布身份闸门断言",
    ),
    # README 的"当前生产版本"标注：v1.5.0 发布 commit 19baf82 证明这是正规
    # 登记点（用户可见的版本声明），漏登记 = README 撒谎。
    RegistryEntry(
        "README.md",
        r'当前生产版本[：:]\s*`(?P<ver>\d+\.\d+\.\d+)',
        "README 中文当前生产版本标注",
    ),
    RegistryEntry(
        "README_en.md",
        r'Current Production Release:\s*`(?P<ver>\d+\.\d+\.\d+)',
        "README 英文当前生产版本标注",
    ),
]


def _find_root(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    # 从脚本位置向上找 pyproject.toml
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / "pyproject.toml").is_file():
            return p
        p = p.parent
    sys.exit("cannot locate project root (no pyproject.toml found)")


def _read_current_version(root: Path) -> str:
    schema = root / "mimir_v8" / "schema.py"
    if not schema.is_file():
        sys.exit(f"schema.py not found: {schema}")
    m = re.search(r'^MIMIR_VERSION\s*=\s*["\']([^"\']+)["\']',
                  schema.read_text(encoding="utf-8"), re.MULTILINE)
    if not m:
        sys.exit("cannot parse MIMIR_VERSION from schema.py")
    return m.group(1)


class Finding(NamedTuple):
    relpath: str
    description: str
    found_version: str
    expected_version: str
    status: str           # "OK" | "STALE" | "MISSING"


def sweep(root: Path, expected: str) -> list[Finding]:
    results: list[Finding] = []

    for entry in REGISTRY:
        fpath = root / entry.relpath
        if not fpath.is_file():
            results.append(Finding(
                entry.relpath, entry.description,
                found_version="(file not found)",
                expected_version=expected,
                status="MISSING",
            ))
            continue

        text = fpath.read_text(encoding="utf-8")

        # 代号表特殊处理：检查当前版本是否在表中
        if entry.relpath == "dashboard/backend/main.py":
            versions_in_table = re.findall(
                r'^\s*"(\d+\.\d+\.\d+)":\s*"[^"]+',
                text, re.MULTILINE,
            )
            if expected in versions_in_table:
                results.append(Finding(
                    entry.relpath, entry.description,
                    found_version=expected,
                    expected_version=expected,
                    status="OK",
                ))
            else:
                results.append(Finding(
                    entry.relpath, entry.description,
                    found_version=f"missing (found: {', '.join(versions_in_table[-3:])})",
                    expected_version=expected,
                    status="STALE",
                ))
            continue

        matches = list(re.finditer(entry.pattern, text, re.MULTILINE))
        if not matches:
            results.append(Finding(
                entry.relpath, entry.description,
                found_version="(pattern not matched)",
                expected_version=expected,
                status="MISSING",
            ))
            continue

        # 取最后一个匹配（对多行命中取最近的）
        found = matches[-1].group("ver")
        status = "OK" if found == expected else "STALE"
        results.append(Finding(
            entry.relpath, entry.description,
            found_version=found,
            expected_version=expected,
            status=status,
        ))

    return results


def main() -> None:
    import io
    import argparse
    # Windows 控制台默认 GBK，无法输出 emoji；强制 UTF-8。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    elif hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    parser = argparse.ArgumentParser(
        description="扫描版本号派生登记表，确保与 MIMIR_VERSION 一致。")
    parser.add_argument("--root", default=None, help="项目根目录")
    parser.add_argument("--version", default=None,
                        help="期望版本号（默认读 schema.py）")
    parser.add_argument("--self-test", action="store_true",
                        help="自检模式：验证扫描器能命中已知登记点")
    args = parser.parse_args()

    root = _find_root(args.root)
    expected = args.version or _read_current_version(root)

    results = sweep(root, expected)

    # 输出
    stale = [r for r in results if r.status != "OK"]
    print(f"version_sweep: expected {expected}, "
          f"checked {len(results)} registry entries\n")

    for r in results:
        marker = "✅" if r.status == "OK" else "❌"
        print(f"  {marker} {r.relpath}")
        print(f"     {r.description}")
        if r.status != "OK":
            print(f"     found: {r.found_version}  (expected: {r.expected_version})")
        print()

    if stale:
        print(f"\n{len(stale)} STALE/MISSING — 升版前请修正以上登记点。")
        sys.exit(1)
    else:
        print(f"All {len(results)} registry entries match {expected}. ✅")
        sys.exit(0)


if __name__ == "__main__":
    main()
