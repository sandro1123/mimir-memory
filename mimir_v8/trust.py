"""Mímir v9.2 Trust Scoring — feedback-driven trust scores for facts.

Trust scores are updated based on:
- Explicit feedback (useful/incorrect/stale/duplicate/harmful)
- Implicit signals (approve/reject, query frequency, manual remember/forget)
- Time decay (trust decays slowly if not accessed)

The trust score is a value between 0.0 and 1.0, used in query ranking.
"""

from __future__ import annotations

import json
import math
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .store import CanonicalStore, canonical_json, new_id, sha256_text, utc_now


# Signal weights — how much each signal type affects trust
SIGNAL_WEIGHTS: dict[str, float] = {
    "feedback.useful": 0.12,
    "feedback.incorrect": -0.15,
    "feedback.stale": -0.10,
    "feedback.duplicate": -0.05,
    "feedback.harmful": -0.20,
    "review.approve": 0.08,
    "review.reject": -0.08,
    "memory.remember": 0.15,
    "memory.forget": -0.12,
    "query.hit": 0.02,
    "query.miss": -0.01,
}

# Maximum cumulative trust change per signal
MAX_DELTA = 0.25
MIN_TRUST = 0.0
MAX_TRUST = 1.0
BASE_TRUST = 0.5

# Trust decay per day if not accessed
TRUST_DECAY_PER_DAY = 0.002


class TrustScore:
    """Mutable trust score with update tracking."""

    def __init__(self, score: float = BASE_TRUST, access_count: int = 0, last_updated: str | None = None):
        self.score = max(MIN_TRUST, min(MAX_TRUST, score))
        self.access_count = access_count
        self.last_updated = last_updated or utc_now()

    def apply_signal(self, signal_type: str, fact_type: str | None = None) -> float:
        """Apply a signal and return the new trust score."""
        delta = SIGNAL_WEIGHTS.get(signal_type, 0.0)
        # Iron rules and preferences are less affected by negative signals
        if delta < 0 and fact_type in ("iron_rule", "user_pref"):
            delta *= 0.3
        # Clamp delta
        delta = max(-MAX_DELTA, min(MAX_DELTA, delta))
        self.score = max(MIN_TRUST, min(MAX_TRUST, self.score + delta))
        self.access_count += 1
        self.last_updated = utc_now()
        return self.score

    def apply_time_decay(self, now: datetime | None = None) -> float:
        """Apply time-based decay to trust score."""
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            last = datetime.fromisoformat(self.last_updated)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days = (now - last).days
            if days > 0:
                self.score = max(MIN_TRUST, self.score - days * TRUST_DECAY_PER_DAY)
        except Exception:
            pass
        return self.score


class TrustManager:
    """Manages trust scores for facts, reading signals from feedback and reviews."""

    def __init__(self, store: CanonicalStore):
        self.store = store

    def update_from_signals(self, dry_run: bool = False) -> dict:
        """Read all unprocessed signals and update fact trust scores."""
        from .schema import DECAY_TIER_MAP
        now = utc_now()
        updated = 0
        errors = []

        with closing(self.store.connect()) as connection:
            # Get all active facts with their current trust scores
            facts = connection.execute(
                "SELECT fact_id, fact_type, confidence_score, updated_at FROM facts WHERE status='active'"
            ).fetchall()

            for fact in facts:
                fact_id = fact["fact_id"]
                fact_type = fact["fact_type"]
                current_conf = fact["confidence_score"] if fact["confidence_score"] is not None else BASE_TRUST

                # Gather signals from feedback table
                feedbacks = connection.execute(
                    "SELECT feedback_type FROM learning_feedback WHERE fact_id=? AND created_at > ?",
                    (fact_id, fact["updated_at"]),
                ).fetchall()

                # Gather signals from review_actions (via candidate_facts)
                candidates = connection.execute(
                    "SELECT c.candidate_id FROM candidate_facts c WHERE c.committed_fact_id=? AND c.updated_at > ?",
                    (fact_id, fact["updated_at"]),
                ).fetchall()

                if not feedbacks and not candidates:
                    continue

                trust = TrustScore(score=current_conf)

                for fb in feedbacks:
                    trust.apply_signal(f"feedback.{fb['feedback_type']}", fact_type)

                for c in candidates:
                    reviews = connection.execute(
                        "SELECT action FROM review_actions WHERE candidate_id=? AND created_at > ?",
                        (c["candidate_id"], fact["updated_at"]),
                    ).fetchall()
                    for r in reviews:
                        if r["action"] == "approve":
                            trust.apply_signal("review.approve", fact_type)
                        elif r["action"] == "reject":
                            trust.apply_signal("review.reject", fact_type)

                trust.apply_time_decay()

                if not dry_run:
                    try:
                        with self.store.connect() as conn:
                            conn.execute(
                                "UPDATE facts SET confidence_score=?, updated_at=? WHERE fact_id=? AND status='active'",
                                (trust.score, now, fact_id),
                            )
                        updated += 1
                    except Exception as e:
                        errors.append({"fact_id": fact_id, "error": str(e)})

        return {"updated": updated, "errors": errors, "dry_run": dry_run}

    def get_trust(self, fact_id: str) -> float:
        """Get the current trust score for a fact."""
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT confidence_score FROM facts WHERE fact_id=?", (fact_id,)
            ).fetchone()
        if row and row["confidence_score"] is not None:
            return row["confidence_score"]
        return BASE_TRUST
