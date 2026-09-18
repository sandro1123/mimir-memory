# -*- coding: utf-8 -*-
"""金标扩容前的只读候选筛查：marker 全表唯一性 + 既有集类型分布对账。

判据：
1. 每条候选 marker 在 active 全表必须恰命中 1 条（其锚 fact 自身）——
   marker 兜底路径撞多行 = benchmark 假绿（把别的 fact 排序也算命中）。
2. 既有 8 条的类型分布决定新增 16 条的四型配平（12 个 fact_type 值按
   四类组：铁律组 / 偏好组 / 配置组 / 模式组）。
"""
import os
import pathlib
import sqlite3
import sys

def _find_prod_db() -> str:
    """定位生产 canonical.db：v9 目录下唯一实例，实例名不落字面量
    （发布闸门 machine_path/payload_sample 面会拦部署标识）。"""
    root = pathlib.Path.home() / ".hermes/mimir/v9"
    hits = sorted(root.glob("*/canonical.db"))
    if len(hits) != 1:
        raise SystemExit(f"预期 v9 下恰一个 canonical.db，实际 {len(hits)}: "
                         f"{[str(h) for h in hits]}")
    return str(hits[0])


DB = os.environ.get("MIMIR_GOLDEN_DB") or _find_prod_db()

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from mimir_v8.eval_suite import GOLDEN_SET  # 单一事实源：直接读金标本体

EXISTING = [(fid[:8], marker) for _q, fid, marker in GOLDEN_SET[:8]]
CANDIDATES = [(fid[:8], "see-eval_suite", marker)
              for _q, fid, marker in GOLDEN_SET[8:]]


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
        ok = n == 1 and s == 1
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
