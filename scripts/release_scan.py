# -*- coding: utf-8 -*-
"""Mímir 发布脱敏扫描器（P0-C, v14.2 七面闸门）。

v10 硬编码 key 被开源脱敏的事故根子预防：泄漏不靠人眼，靠机器闸门。
每次 push 跑一遍（.github/workflows/ci.yml 的 release-scan job），
也供本地预检：python scripts/release_scan.py [--root DIR]。

七面（词表外置 release_scan_rules.txt，勿硬编码——否则词表本身
命中 username 面）：secrets / private_ip / machine_path / username /
pem_key(并入 secrets 词表) / payload_sample / 负向对照（测试覆盖）。

设计纪律：
- 词表文件自身免疫（扫描时跳过词表与扫描器自身）
- 误杀代价 = 贡献者被烦死，漏杀代价 = 事故——规则宁窄勿宽，
  CLEAN 案例必须恒绿（见 tests/test_p0c_release_scan.py）
"""
from __future__ import annotations

import re
import sys
from fnmatch import fnmatch
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "release_scan_rules.txt"

# 扫描范围：仓库内全部文本文件，但跳过这些（二进制/自身/词表/venv 产物）
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
             ".pytest_cache", ".ruff_cache", ".mypy_cache", "*.egg-info"}
SKIP_FILES = {RULES_PATH.name, "release_scan.py"}
# 豁免：测试与负向对照件携带的是「故意」样本——它们正是闸门的自检物
# （闸门失效时这些文件恰恰必须被抓到，CI 里由单测覆盖，全树扫描时跳过）。
SKIP_EXEMPT = {"test_p0c_release_scan.py", "release_scan_negative_control.md",
               "test_p16_bruteforce.py"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".yml", ".yaml", ".toml", ".json",
                 ".cfg", ".ini", ".sh", ".js", ".html", ".css", ".sql",
                 ".service", ".timer", ""}


def load_rules(path: Path = RULES_PATH) -> list[tuple[str, re.Pattern]]:
    """读词表：face<TAB>regex 每行一条；注释/空行跳过。"""
    rules: list[tuple[str, re.Pattern]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        face, _, pattern = line.partition("\t")
        if not pattern:
            continue
        rules.append((face, re.compile(pattern)))
    return rules


class Scanner:
    """七面扫描器。face 由词表定义；命中按 (face, pattern, line_no, excerpt) 上报。"""

    def __init__(self, rules: list[tuple[str, re.Pattern]] | None = None):
        self.rules = rules if rules is not None else load_rules()

    def scan_text(self, text: str, *, path: str = "<text>") -> list[dict]:
        hits: list[dict] = []
        for face, pattern in self.rules:
            for m in pattern.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                hits.append({
                    "face": face,
                    "pattern": pattern.pattern,
                    "path": path,
                    "line": line_no,
                    "excerpt": text[max(0, m.start() - 20):m.end() + 20]
                    .replace("\n", " ")[:80],
                })
        hits.sort(key=lambda h: (h["path"], h["line"], h["face"]))
        return hits

    def scan_file(self, path: Path) -> list[dict]:
        return self.scan_text(path.read_text(encoding="utf-8", errors="replace"),
                              path=str(path))

    def scan_tree(self, root: Path) -> dict:
        files_scanned = 0
        all_hits: list[dict] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS or fnmatch(part, "*.egg-info")
                   for part in path.parts):
                continue
            if path.name in SKIP_FILES or path.name in SKIP_EXEMPT:
                continue
            if path.suffix not in TEXT_SUFFIXES:
                continue
            files_scanned += 1
            all_hits.extend(self.scan_file(path))
        return {"files_scanned": files_scanned, "hits": all_hits,
                "hit_count": len(all_hits)}


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    stats = Scanner().scan_tree(root)
    print(f"scanned {stats['files_scanned']} files under {root}")
    for hit in stats["hits"]:
        print(f"HIT [{hit['face']}] {hit['path']}:{hit['line']} :: {hit['excerpt']}")
    if stats["hit_count"]:
        print(f"\n{stats['hit_count']} hit(s) — 发布阻断（脱敏后重跑）")
        return 1
    print("clean — 发布闸门放行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
