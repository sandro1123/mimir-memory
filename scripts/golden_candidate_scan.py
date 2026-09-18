# -*- coding: utf-8 -*-
"""金标扩容前的只读候选筛查：marker 全表唯一性 + 既有集类型分布对账。

判据：
1. 每条候选 marker 在 active 全表必须恰命中 1 条（其锚 fact 自身）——
   marker 兜底路径撞多行 = benchmark 假绿（把别的 fact 排序也算命中）。
2. 既有 8 条的类型分布决定新增 16 条的四型配平（12 个 fact_type 值按
   四类组：铁律组 / 偏好组 / 配置组 / 模式组）。
"""
import pathlib
import sqlite3
import sys

DB = (str(pathlib.Path.home() / ".hermes/mimir/v9"
        / "production-v9.0-20260805_214614/canonical.db"))

EXISTING = [
    ("dad7aea2", "运维职责"),
    ("4389e49d", "N100 内存过载"),
    ("4bddde4a", "Mímir维护需定期检查这些场景"),
    ("2de24c79", "早间新闻"),
    ("57f4c028", "回复卡片header改为Heimdallr-EX"),
    ("789eb5c9", "倾向于独立记忆管理"),
    ("7fce0a72", "总觉得我的obsidian笔记库乱七八糟"),
    ("8e2e6a41", "文件发送必须在当前会话中完成"),
]

CANDIDATES = [
    ("96ca3bbe", "iron_rule", "存算分离"),
    ("71031656", "iron_rule", "S.C.O.R.E"),
    ("c53ac24a", "iron_rule", "低熵JSON信封"),
    ("a993fca5", "iron_rule", "全局搜索铁律"),
    ("ff61a756", "user_pref", "禁止修改QuantStar代码"),
    ("20b8ad3c", "user_pref", "Reactor Atlas"),
    ("55589276", "user_pref", "峰谷时段规则"),
    ("14b0c6cd", "user_pref", "合在一个相框"),
    ("7480c8b8", "project_config", "技能落地位置"),
    ("765b8aa6", "project_config", "neural-band-poc"),
    ("0c3eb3b0", "project_config", "漂移哨兵P0-7"),
    ("1750483c", "project_config", "五通道路由"),
    ("d5d22557", "pattern", "全量审计prompt"),
    ("b16f032d", "pattern", "免费模型可能被映射"),
    ("0b8dd824", "pattern", "unexpected error"),
    ("19ea85cd", "pattern", "qwen3.8max-free可联通"),
]


def main() -> int:
    conn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def active_hits(marker: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM facts WHERE status='active' AND ("
            "instr(lower(COALESCE(summary,'')), ?) > 0"
            " OR instr(lower(COALESCE(content,'')), ?) > 0)",
            (marker.lower(), marker.lower()),
        ).fetchone()[0]

    def self_hit(fid: str, marker: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM facts WHERE fact_id LIKE ? AND status='active'"
            " AND (instr(lower(COALESCE(summary,'')), ?) > 0"
            " OR instr(lower(COALESCE(content,'')), ?) > 0)",
            (fid + "%", marker.lower(), marker.lower()),
        ).fetchone()[0]

    def fact_type(fid: str):
        row = conn.execute(
            "SELECT fact_type, owner_principal FROM facts WHERE fact_id LIKE ?",
            (fid + "%",)).fetchone()
        return (row["fact_type"], row["owner_principal"]) if row else (None, None)

    bad = 0
    print("== 既有 8 条（类型 + marker 唯一性复核） ==")
    from collections import Counter
    types = Counter()
    for fid, marker in EXISTING:
        ft, owner = fact_type(fid)
        types[ft] += 1
        n = active_hits(marker)
        ok = n == 1 and self_hit(fid, marker) == 1
        bad += not ok
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] hits={n} type={ft:<14} owner={owner:<11} {marker}")
    print("  既有类型分布:", dict(types))

    print("== 候选 16 条 ==")
    cand_types = Counter()
    for fid, expected_type, marker in CANDIDATES:
        ft, owner = fact_type(fid)
        n = active_hits(marker)
        s = self_hit(fid, marker)
        ok = n == 1 and s == 1 and ft == expected_type
        bad += not ok
        cand_types[ft] += 1
        flag = "OK " if ok else "BAD"
        print(f"  [{flag}] hits={n} self={s} type={ft:<14} "
              f"(预期{expected_type}) owner={owner:<11} {marker}")
    print("  候选类型分布:", dict(cand_types))
    total = types + cand_types
    print("== 合并 24 条类型分布 ==")
    print("  ", dict(total))
    print("RESULT:", "ALL-UNIQUE" if bad == 0 else f"{bad} 条不合格(换marker)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
