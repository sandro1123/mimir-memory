"""Mímir v9.2 Jaccard similarity deduplication for candidate facts.

When a new candidate is created, compare it against existing active facts
and previously committed candidates using Jaccard similarity on tokenized content.

Decision matrix:
  - similarity >= 0.85 → auto-merge (update existing fact, discard candidate)
  - similarity >= 0.70 → mark as candidate update (requires review)
  - similarity < 0.70  → new fact (no action)
"""

from __future__ import annotations

import re
from contextlib import closing
from typing import Any

from .store import CanonicalStore


JACCARD_MERGE_THRESHOLD = 0.85
JACCARD_UPDATE_THRESHOLD = 0.70


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
                 limit: int = 50) -> list[dict]:
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
        if not pool:
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


def check_duplicate(store: CanonicalStore, content: str, owner: str) -> dict:
    """Check if content is a duplicate of an existing active fact or committed candidate.

    Returns:
        {
            "is_duplicate": bool,
            "similarity": float,
            "match_type": "merge" | "update" | "new",
            "matched_fact_id": str | None,
            "matched_content": str | None,
        }
    """
    if not content or not content.strip():
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
    pool = _recall_pool(store, content, owner, limit=50)

    for row in pool:
        score = jaccard_similarity(content, row["content"])
        if score > best_score:
            best_score = score
            best_id = row["fact_id"]
            best_content = row["content"]

    if best_score >= JACCARD_MERGE_THRESHOLD:
        return {"is_duplicate": True, "similarity": round(best_score, 4), "match_type": "merge", "matched_fact_id": best_id, "matched_content": best_content}
    elif best_score >= JACCARD_UPDATE_THRESHOLD:
        return {"is_duplicate": True, "similarity": round(best_score, 4), "match_type": "update", "matched_fact_id": best_id, "matched_content": best_content}
    else:
        return {"is_duplicate": False, "similarity": round(best_score, 4), "match_type": "new", "matched_fact_id": None, "matched_content": None}
