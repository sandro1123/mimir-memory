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
    return tokens


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

    with closing(store.connect()) as connection:
        # Check active facts
        facts = connection.execute(
            "SELECT fact_id, content FROM facts WHERE status='active' AND owner_principal=?",
            (owner,),
        ).fetchall()
        for row in facts:
            score = jaccard_similarity(content, row["content"])
            if score > best_score:
                best_score = score
                best_id = row["fact_id"]
                best_content = row["content"]

        # Check committed candidates
        candidates = connection.execute(
            "SELECT candidate_id, content FROM candidate_facts WHERE status='committed' AND proposed_owner_principal=?",
            (owner,),
        ).fetchall()
        for row in candidates:
            score = jaccard_similarity(content, row["content"])
            if score > best_score:
                best_score = score
                best_id = row["candidate_id"]
                best_content = row["content"]

    if best_score >= JACCARD_MERGE_THRESHOLD:
        return {"is_duplicate": True, "similarity": round(best_score, 4), "match_type": "merge", "matched_fact_id": best_id, "matched_content": best_content}
    elif best_score >= JACCARD_UPDATE_THRESHOLD:
        return {"is_duplicate": True, "similarity": round(best_score, 4), "match_type": "update", "matched_fact_id": best_id, "matched_content": best_content}
    else:
        return {"is_duplicate": False, "similarity": round(best_score, 4), "match_type": "new", "matched_fact_id": None, "matched_content": None}
