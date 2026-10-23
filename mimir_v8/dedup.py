"""Mímir v9.2 Jaccard similarity deduplication for candidate facts.

三档信任分层（roadmap #16，issue #10）：

- Tier-0 确定性静默合并：content_hash 精确命中即 merge。O(1)，
  不依赖分词/召回池，不受冷启动门限制（确定性与样本量无关）。
- Tier-1 模糊判重（Jaccard）：受 ``is_fuzzy_ready`` 冷启动门控；
  样本不足时宁可漏报，不误杀。命中只给"候选更新"语义（只准 ADD，
  不自动改写——写路径契约本卡不动）。
- Tier-2 判断类：永不自动执行，走 human_review（见 review.py），
  本模块不覆盖。

既有决策矩阵（Tier-1 内部分带）：
  - similarity >= 0.85 → merge
  - similarity >= 0.70 → update（requires review）
  - similarity < 0.70  → new
"""

from __future__ import annotations

import os
import re
from contextlib import closing
from typing import Any

from .store import CanonicalStore, sha256_text


JACCARD_MERGE_THRESHOLD = 0.85
JACCARD_UPDATE_THRESHOLD = 0.70


def is_fuzzy_ready(store: CanonicalStore, owner: str, *, min_docs: int = 10) -> bool:
    """冷启动门（roadmap #16）：owner 名下 active 事实不足 min_docs 时，
    不启动模糊判重——宁可漏报，不误杀。

    Tier-0（content_hash 精确命中）不受此门限制：它是确定性的，
    与样本量无关。
    """
    if isinstance(min_docs, bool) or not isinstance(min_docs, int) or min_docs < 1:
        raise ValueError("min_docs must be a positive integer")
    with closing(store.connect()) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM facts WHERE status='active' AND owner_principal=?",
            (owner,),
        ).fetchone()[0]
    return int(count) >= min_docs


def _tier0_exact(store: CanonicalStore, content: str, owner: str) -> dict | None:
    """Tier-0 确定性精确命中（roadmap #16）：content_hash 等值，O(1)。

    口径与写入路径一致：``CreateFact.validated`` 先 strip 再
    ``sha256_text``，所以这里必须 hash strip 后的文本，否则带首尾
    空白的同内容永远对不上。

    只查 owner 名下的 active facts + committed candidates（ACL 隔离，
    跨 owner 同内容不得合并）。不受 ``is_fuzzy_ready`` 冷启动门限制。
    """
    stripped = content.strip()
    digest = sha256_text(stripped)
    with closing(store.connect()) as connection:
        row = connection.execute(
            """SELECT fact_id, content FROM facts
            WHERE status='active' AND owner_principal=?
              AND content_hash=?
            LIMIT 1""",
            (owner, digest),
        ).fetchone()
        if row:
            return {"fact_id": row[0], "content": row[1]}
        crow = connection.execute(
            """SELECT candidate_id, content FROM candidate_facts
            WHERE status='committed' AND proposed_owner_principal=?
              AND content=?
            LIMIT 1""",
            (owner, stripped),
        ).fetchone()
        if crow:
            return {"fact_id": crow[0], "content": crow[1]}
    return None


_GATE_MODES = ("warn", "block_strong", "off")


def gate_mode() -> str:
    """审批闸工作模式（issue #11 第 2 步）。

    观察期已满：2026-10-22 实测生产 `human_review` 积压 15 条中携带
    ``possible_duplicate`` 标记的 **0 条**——噪声为零，具备收紧条件。
    但收紧仍是**显式开关**，默认不动：

      - ``warn``（默认）：只留审计痕，不拦（与从前逐字节一致）
      - ``block_strong``：Tier-0 精确重复**拒绝**审批；Tier-1 模糊仍只告警
      - ``off``：完全静默（紧急回退用）

    未知取值直接抛错——开关写错却静默退回默认，会让人以为"我开了拦阻"
    而实际仍在放行。
    """
    raw = os.environ.get("MIMIR_DEDUP_GATE", "warn").strip().lower()
    if raw not in _GATE_MODES:
        raise ValueError(
            f"invalid MIMIR_DEDUP_GATE={raw!r}; expected one of {_GATE_MODES}")
    return raw


def duplicate_review_gate(hint: dict | None) -> dict:
    """Tier-1 ADD-only 闸（roadmap #16）：把判重 hint 翻译成审批期裁决。

    **纯函数**：不读库、不写库、不做决定——只给"该不该喊、喊多大声"。
    真正的接线在 `candidates.py:review_candidate`（留 audit 痕）。

    warn-first：本闸**不阻断**审批（观察期）。它存在的意义是让
    ``possible_duplicate`` 标记有消费方——写进 uncertainty_json 却没人看，
    等于没接线。

    Level 分级：
      - ``strong_warn``：Tier-0 精确命中（merge + tier0），同一内容重复落库
      - ``warn``       ：Tier-1 模糊命中（update/merge）
      - ``None``       ：无标记
    """
    if not hint or not hint.get("match_type"):
        return {"flagged": False, "level": None, "match_type": None,
                "similarity": None, "matched_fact_id": None}

    match_type = str(hint.get("match_type"))
    tier = hint.get("tier")
    # Tier-0 精确 = 内容逐字相同，比"相似"重得多；未标 tier 的 merge
    # 也按强告警处理（旧数据没有 tier 字段，语义上 merge 本就最强）。
    if match_type == "merge" and tier in (None, "tier0"):
        level = "strong_warn"
    else:
        level = "warn"

    return {
        "flagged": True,
        "level": level,
        "match_type": match_type,
        "similarity": hint.get("similarity"),
        "matched_fact_id": hint.get("matched_fact_id"),
    }


def _tokenize(text: str) -> set[str]:
    """Tokenize Chinese + English text into a set of tokens."""
    if not text:
        return set()
    text = text.lower().strip()
    # Split Chinese characters into individual characters
    tokens: set[str] = set()
    # English words
    for word in re.findall(r"[a-z][a-z0-9_]*", text):
        tokens.add(word)
    # Chinese bigrams
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(chars) - 1):
        tokens.add(chars[i] + chars[i + 1])
    # Single Chinese chars for short texts
    if len(chars) <= 4:
        for c in chars:
            tokens.add(c)
    # P0-M (dudu Y2): numeric/version/model segments keep their identity —
    # "Gemini-3.8-Flash" vs "Gemini-4.0-Pro" must differ meaningfully.
    for num in re.findall(r"\d+(?:\.\d+)+", text):      # 3.8 / 1.5.9
        tokens.add(num)
    for ver in re.findall(r"v\d+(?:\.\d+)*", text):      # v14.2
        tokens.add(ver)
    for model in re.findall(r"[a-z]+-\d[\d.a-z]*(?:-[a-z0-9.]+)+", text):
        # gemini-3.8-flash style compound model ids (lowercased already)
        tokens.add(model)
    return tokens


def _recall_pool(store: CanonicalStore, content: str, owner: str, *,
                 limit: int = 50, allow_full_scan: bool = True) -> list[dict]:
    """Candidate pool for exact dedup: distinctive-token LIKE prefilter.

    Uses the longest tokens of the incoming content as recall probes
    against both active facts and committed candidates, unioned and
    capped at `limit`. Falls back to the full owner scan when the probe
    pool is empty but the owner holds facts (correctness first).
    """
    tokens = sorted(_tokenize(content), key=len, reverse=True)
    probes = tokens[:6]  # longest distinctive tokens
    seen: dict[str, dict] = {}

    def _absorb(rows) -> None:
        for row in rows:
            fid, cont = row[0], row[1]
            if fid and fid not in seen:
                seen[fid] = {"fact_id": fid, "content": cont}

    with closing(store.connect()) as connection:
        for probe in probes:
            _absorb(connection.execute(
                """SELECT fact_id, content FROM facts
                WHERE status='active' AND owner_principal=?
                  AND content LIKE ?
                LIMIT ?""",
                (owner, f"%{probe}%", limit),
            ).fetchall())
            _absorb(connection.execute(
                """SELECT candidate_id, content FROM candidate_facts
                WHERE status='committed' AND proposed_owner_principal=?
                  AND content LIKE ?
                LIMIT ?""",
                (owner, f"%{probe}%", limit),
            ).fetchall())
        pool = list(seen.values())[:limit]
        if not pool and allow_full_scan:
            count = connection.execute(
                "SELECT COUNT(*) FROM facts WHERE status='active' AND owner_principal=?",
                (owner,),
            ).fetchone()[0]
            if count:
                # degradation: probe missed but facts exist — full scan
                for row in connection.execute(
                    "SELECT fact_id, content FROM facts WHERE status='active' AND owner_principal=?",
                    (owner,),
                ).fetchall():
                    seen[row[0]] = {"fact_id": row[0], "content": row[1]}
                pool = list(seen.values())
    return pool


def jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two strings."""
    set_a = _tokenize(a)
    set_b = _tokenize(b)
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def check_duplicate(store: CanonicalStore, content: str, owner: str, *,
                    allow_full_scan: bool = True,
                    fuzzy_min_docs: int | None = None) -> dict:
    """Check if content is a duplicate of an existing active fact or committed candidate.

    ``allow_full_scan=False`` 关闭「probe 落空则全表扫描」的正确性回退：
    观察期调用方（候选写入热路径）用它在「召回不到」与「扫全表」之间选前者——
    LIKE '%probe%' 无索引可用，重复内容多时全扫可达数百毫秒，观察件不值得
    让生产写入等它。默认 True 保持既有语义（独立调用方的正确性优先）。

    ``fuzzy_min_docs=None``（默认）关闭冷启动门，行为与从前一致（零破坏）；
    传正整数即打开 Tier-1 门控：owner 名下 active 事实不足该数时，模糊判重
    让路（返回 new），Tier-0 精确命中不受此门限制。

    Returns:
        {
            "is_duplicate": bool,
            "similarity": float,
            "match_type": "merge" | "update" | "new",
            "matched_fact_id": str | None,
            "matched_content": str | None,
            "tier": "tier0" | "tier1",  # 只在命中时出现
        }
    """
    if not content or not content.strip():
        return {"is_duplicate": False, "similarity": 0.0, "match_type": "new", "matched_fact_id": None, "matched_content": None}

    # Tier-0 确定性精确命中：O(1)，不依赖分词/召回池，不受冷启动门限制。
    exact = _tier0_exact(store, content, owner)
    if exact is not None:
        return {"is_duplicate": True, "similarity": 1.0, "match_type": "merge",
                "matched_fact_id": exact["fact_id"], "matched_content": exact["content"],
                "tier": "tier0"}

    # Tier-1 门控：样本不足时宁可漏报，不误杀。
    # bool 是 int 子类，先挡掉（否则 True 会被当成 min_docs=1）。
    if fuzzy_min_docs is not None:
        if isinstance(fuzzy_min_docs, bool) or not isinstance(fuzzy_min_docs, int) or fuzzy_min_docs < 1:
            raise ValueError("fuzzy_min_docs must be a positive integer or None")
        if not is_fuzzy_ready(store, owner, min_docs=fuzzy_min_docs):
            return {"is_duplicate": False, "similarity": 0.0, "match_type": "new", "matched_fact_id": None, "matched_content": None}

    best_score = 0.0
    best_id = None
    best_content = None

    # P0-M (dudu Y1): recall layer before exact Jaccard. Full-table scan
    # was O(N) per candidate write; instead pull a top-K candidate pool
    # via a LIKE prefilter over distinctive tokens, then exact-Jaccard
    # only that pool. Degradation rule: if the pool somehow comes back
    # empty AND the owner has facts, fall back to the full scan —
    # correctness beats performance.
    pool = _recall_pool(store, content, owner, limit=50,
                        allow_full_scan=allow_full_scan)

    for row in pool:
        score = jaccard_similarity(content, row["content"])
        if score > best_score:
            best_score = score
            best_id = row["fact_id"]
            best_content = row["content"]

    if best_score >= JACCARD_MERGE_THRESHOLD:
        return {"is_duplicate": True, "similarity": round(best_score, 4), "match_type": "merge", "matched_fact_id": best_id, "matched_content": best_content, "tier": "tier1"}
    elif best_score >= JACCARD_UPDATE_THRESHOLD:
        return {"is_duplicate": True, "similarity": round(best_score, 4), "match_type": "update", "matched_fact_id": best_id, "matched_content": best_content, "tier": "tier1"}
    else:
        return {"is_duplicate": False, "similarity": round(best_score, 4), "match_type": "new", "matched_fact_id": None, "matched_content": None}
