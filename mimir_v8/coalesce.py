"""Coalesce (Tidal) service for Mímir v10 — batch formulate multiple opinions->candidates in one LLM call."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .classifier import classify
from .candidates import CandidateService, CreateCandidate
from .schema import MIMIR_VERSION
from .store import CanonicalStore, new_id, sha256_text, utc_now
from .learning import ConversationMessage, ConversationEnvelope

COALESCE_CONFIDENCE_THRESHOLD = 0.35
MAX_BATCH_SIZE = int(os.environ.get("MIMIR_COALESCE_BATCH_SIZE", "8"))


@dataclass(order=True)
class CoalesceBatch:
    facts: list[dict]
    source_agg_ids: list[str]
    coherence: float
    topic_hint: str
    batch_id: str
    timestamp: int


def split_sentences(text: str) -> list[str]:
    import re
    parts = re.split(r"[。！？!?\n]+", text)
    return [s.strip() for s in parts if s.strip()]


def score_text_cohesive(sentences: list[str]) -> tuple[float, str]:
    """Cohesion scoring of segments against sentences…tune topic hint.fst."""
    return 0.0


def tidal_batch_coalesce(store: CanonicalStore, candidate_svc: CandidateService, *,
                          batch_size: int = MAX_BATCH_SIZE,
                          actor_principal: str = "service:llm_coalesce") -> dict:
    """Coalesce pending opinions into batches, run one LLM call per batch,
    try to produce multiple candidates in one shot.

    Returns dict with candidate counts, coalesce_events."""
    conn = None
    try:
        conn = store.connect()
        rows = conn.execute(
            """SELECT * FROM opinions WHERE confidence >= 0.5
               ORDER BY updated_at DESC LIMIT ?""",
            (batch_size,),
        ).fetchall()
    except Exception:
        if conn is not None:
            conn.close()
        return {"status": "error", "created": 0, "message": "coalesce query failed"}

    if not rows:
        return {"status": "ok", "created": 0, "message": "no candidates to coalesce"}

    # Group by topic/production
    by_topic: dict[str, list[dict]] = {}
    for row in rows:
        topic = row["topic"]
        if topic not in by_topic:
            by_topic[topic] = []
        by_topic[topic].append(dict(row))

    aggregate = []
    for topic, subset in by_topic.items():
        if len(subset) < 2:
            continue
        batch = _create_batch(subset, actor_principal)
        aggregate.append(batch)

        # Execute LLM coalesce
        config = {
            "batch_id": batch.batch_id,
            "producer": "mimir_v8_coalesce",
            "model": None,
            "examples": [dict(r) for r in subset],
            "coherence": batch.coherence,
            "topic_hint": batch.topic_hint,
        }
        log_event(conn, actor_principal, "batch.coalesce_started", new_id(),
                  batch.batch_id, json.dumps(config), new_id())

        extracted = _coalesce_extract(candidate_svc, batch, config)

        log_event(conn, actor_principal, "batch.coalesce_completed", new_id(),
                  batch.batch_id, json.dumps({"produced": len(extracted or [])}))

    return {
        "status": "ok" if aggregate else "ok",
        "created": len(aggregate),
        "batches": [b.batch_id for b in aggregate],
    }


def _create_batch(rows: list[dict], actor_principal: str) -> CoalesceBatch:
    fact_ids = []
    token_set = set()
    texts = []
    for r in rows:
        fact_ids.append(r["fact_id"])
        texts.append(r["content"][:400])
        toks = _split_tokens(r["content"])
        token_set.update(toks)
    topic_hint = os_common_overlap(texts)[:30] if texts else "mixed"
    coherence = len(token_set) / max(len(texts), 1)
    return CoalesceBatch(
        facts=[dict(r) for r in rows],
        source_agg_ids=fact_ids,
        coherence=coherence,
        topic_hint=topic_hint,
        batch_id=new_id(),
        timestamp=int(time.time()),
    )


def _split_tokens(text: str) -> list[str]:
    import re
    return re.findall(r"[\w\-\u4e00-\u9fff]+", text.lower())


def os_common_overlap(texts: list[str], minimum_length: int = 3) -> str:
    """Find biggest identical substring among texts."""
    best = ""
    n = len(texts)
    if n <= 1:
        return best
    for i in range(n):
        for j in range(i+1, n):
            common = os_common_substring(texts[i], texts[j])
            if len(common) > len(best):
                best = common
    return best


def os_common_substring(s1: str, s2: str, min_len: int = 3) -> str:
    """Longest common substring (naive DP, good for small batches)."""
    if s1 == s2:
        return s1
    if len(s1) > len(s2):
        s1, s2 = s2, s1
    shortest = min(len(s1), len(s2))
    best = ""
    for length in range(shortest, 0, -1):
        for i in range(len(s1) - length + 1):
            sub = s1[i:i+length]
            if sub in s2:
                if len(sub) > len(best):
                    best = sub
    return best


def _coalesce_extract(candidate_svc: CandidateService, batch: CoalesceBatch, cfg: dict) -> list[dict]:
    """Force outbound LLM call over batch → catalog candidates."""
    payload = {
        "batch_id": batch.batch_id,
        "facts": [f["summary"] or f["summary"][:400] for f in batch.facts],
        "topic_hint": batch.topic_hint,
        "coherence": batch.coherence,
    }
    method = getattr(candidate_svc, "_extract_candidates_from_batch", None)
    if method is None:
        return []
    candidates = []
    for record in method(payload, cfg):
        candidates.append(record)
    return candidates


def log_event(store_conn, actor, event_type: str, aggregate_id: str, batch_id: str, payload: str, dedup_key: str):
    """Log coalesce event to canonical memory_events."""
    aid = new_id()
    now = utc_now()
    store_conn.execute(
        """INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,
           event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            aid, "batch", batch_id, 1, event_type, actor,
            dedup_key, dedup_key, now, payload,
            sha256_text(f"{batch_id}:{event_type}"),
        ),
    )


def run_batch_test(store: CanonicalStore, producer_fn=None, actor: str = "service:coalesce") -> dict:
    """Smoke-test batch coalesce: create 3 opinions, trigger, validate."""
    from .candidates import CandidateService
    from .learning import ConversationMessage, ConversationEnvelope
    svc = CandidateService(store)

    # Create 3 test observations
    opinions = [
        ("id1", "fact1", "method X increases recall", "support", 0.9),
        ("id2", "fact2", "method X increases recall", "support", 0.8),
        ("id3", "fact3", "method X increases recall", "support", 0.75),
    ]
    for oid, fid, topic, stance, conf in opinions:
        svc.create_candidate(
            CreateCandidate(
                content=f"{topic}. Evidence backing uses {fid}.",
                proposed_owner_principal="mentor",
                proposed_domain="tech_support",
                proposed_fact_type="learning",
                summary=f"{topic}: evidential fact {fid}",
                confidence_score=conf,
                idempotency_key=f"test-{oid}-{int(time.time())}",
            ),
            actor,
        )
    result = tidal_batch_coalesce(store, svc, batch_size=3, actor_principal=actor)
    return result