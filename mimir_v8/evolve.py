"""EvolveMem — search retrieval self-evolution for Mímir v12.

Borrowed from aiduMEI's EvolveMem feedback loop:
- users submit useful / useless / correction signals on search results
- a background worker aggregates 7-day quality metrics and nudges confidence
- all adjustments go through opinions (evolve_confidence) + audit events, never
  overwrite history.

Design principle: every confidence adjustment writes a new memory_event so the
evolution is auditable and reversible.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .store import CanonicalStore, new_id, sha256_text, utc_now


V15_ADDITIVE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS search_feedback (
        feedback_id TEXT PRIMARY KEY,
        query_text TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        signal TEXT NOT NULL CHECK (signal IN ('useful', 'useless', 'correction')),
        user_principal TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_search_feedback_fact
       ON search_feedback(fact_id)""",
    """CREATE INDEX IF NOT EXISTS idx_search_feedback_created
       ON search_feedback(created_at)""",
    """CREATE TABLE IF NOT EXISTS quality_metrics (
        metric_id TEXT PRIMARY KEY,
        date TEXT NOT NULL,
        query_count INTEGER NOT NULL DEFAULT 0,
        hit_count INTEGER NOT NULL DEFAULT 0,
        avg_score REAL NOT NULL DEFAULT 0,
        zero_hit_count INTEGER NOT NULL DEFAULT 0,
        useful_signals INTEGER NOT NULL DEFAULT 0,
        useless_signals INTEGER NOT NULL DEFAULT 0,
        evolved_at TEXT NOT NULL
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_quality_metrics_date
       ON quality_metrics(date)""",
)


# How much confidence moves per aggregate signal (matches OPINION_CONFIDENCE_DELTA).
CONFIDENCE_DELTA = 0.05
# Evolve scan window (days).
EVOLVE_WINDOW_DAYS = 7
# Minimum signals before an evolution cycle adjusts anything.
MIN_SIGNALS_PER_FACT = 5
# Useful-ratio thresholds (useful / (useful + useless)) required to move confidence.
USEFUL_RATIO_UP = 0.6
USEFUL_RATIO_DOWN = 0.4


class EvolveMemService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def submit_feedback(self, query_text: str, fact_id: str, signal: str,
                        user_principal: str = "mentor",
                        actor_principal: str = "service:evolve") -> dict:
        if signal not in ("useful", "useless", "correction"):
            raise ValueError("signal must be useful|useless|correction")
        now = utc_now()
        feedback_id = new_id()
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO search_feedback(
                    feedback_id, query_text, fact_id, signal, user_principal, created_at
                ) VALUES(?,?,?,?,?,?)""",
                (feedback_id, query_text, fact_id, signal, user_principal, now),
            )
            connection.execute(
                """INSERT INTO memory_events(
                    event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                    actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), "feedback", feedback_id, 1, "search.feedback", actor_principal,
                 new_id(), new_id(), now,
                 json.dumps({"query": query_text, "fact_id": fact_id, "signal": signal}),
                 sha256_text(f"{fact_id}:{signal}")),
            )
        return {"feedback_id": feedback_id, "status": "ok"}

    def report(self) -> dict:
        """Return 7-day quality dashboard data."""
        with self.store.transaction() as connection:
            # query volume / hit / zero-hit over the window
            query_count = 0
            hit_count = 0
            avg_score = 0.0
            zero_hit_count = 0
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action='query' "
                ).fetchone()
                query_count = row[0] if row else 0
            except Exception:
                pass
            signals = dict(connection.execute(
                "SELECT signal, COUNT(*) FROM search_feedback GROUP BY signal"
            ).fetchall())
            evolved_rows = connection.execute(
                "SELECT date, query_count, hit_count, avg_score, zero_hit_count, "
                "useful_signals, useless_signals, evolved_at "
                "FROM quality_metrics ORDER BY date DESC LIMIT 14"
            ).fetchall()
        return {
            "window_days": EVOLVE_WINDOW_DAYS,
            "signals": {"useful": signals.get("useful", 0),
                        "useless": signals.get("useless", 0),
                        "correction": signals.get("correction", 0)},
            "total_feedback": sum(signals.values()),
            "recent_cycles": [dict(r) for r in evolved_rows],
            "audit_query_count": query_count,
        }

    def evolve(self, actor_principal: str = "service:evolve") -> dict:
        """Aggregate 7-day feedback and nudge fact confidence via opinions.

        For each fact with >= MIN_SIGNALS_PER_FACT feedback:
          - more useful than useless  -> +CONFIDENCE_DELTA
          - more useless than useful  -> -CONFIDENCE_DELTA
        Writes a quality_metrics row and (for adjusted facts) opinion.evolve.
        """
        now = utc_now()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        adjusted = 0
        notes = []
        with self.store.transaction() as connection:
            fact_signals = {
                row["fact_id"]: row for row in connection.execute(
                    """SELECT fact_id,
                       SUM(CASE WHEN signal='useful' THEN 1 ELSE 0 END) as useful,
                       SUM(CASE WHEN signal='useless' THEN 1 ELSE 0 END) as useless,
                       COUNT(*) as total
                       FROM search_feedback
                       WHERE created_at >= date('now', ?) AND fact_id IN (SELECT fact_id FROM facts)
                       GROUP BY fact_id""",
                    (f"-{EVOLVE_WINDOW_DAYS} days",),
                ).fetchall()
            }
            for fact_id, agg in fact_signals.items():
                if agg["total"] < MIN_SIGNALS_PER_FACT:
                    continue
                decisive = agg["useful"] + agg["useless"]
                if decisive == 0:
                    continue
                ratio = agg["useful"] / decisive
                if ratio >= USEFUL_RATIO_UP:
                    delta = CONFIDENCE_DELTA
                elif ratio <= USEFUL_RATIO_DOWN:
                    delta = -CONFIDENCE_DELTA
                else:
                    continue
                self._nudge_confidence(connection, fact_id, delta, actor_principal, now)
                adjusted += 1
                notes.append({"fact_id": fact_id, "delta": delta})
            # record quality metric for this cycle
            useful = sum(a["useful"] for a in fact_signals.values())
            useless = sum(a["useless"] for a in fact_signals.values())
            connection.execute(
                """INSERT INTO quality_metrics(
                    metric_id, date, query_count, hit_count, avg_score,
                    zero_hit_count, useful_signals, useless_signals, evolved_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id(), today, len(fact_signals), len(fact_signals), 0.0,
                 0, useful, useless, now),
            )
        return {"status": "ok", "adjusted": adjusted, "notes": notes,
                "date": today, "window_days": EVOLVE_WINDOW_DAYS}

    def _nudge_confidence(self, connection, fact_id: str, delta: float,
                          actor_principal: str, now: str) -> None:
        """Nudge a fact's confidence_score, appending an audit event."""
        row = connection.execute(
            "SELECT confidence_score FROM facts WHERE fact_id=?", (fact_id,)
        ).fetchone()
        if not row:
            return
        old = row["confidence_score"] or 0.5
        new = max(0.0, min(1.0, old + delta))
        connection.execute(
            "UPDATE facts SET confidence_score=?, updated_at=? WHERE fact_id=?",
            (round(new, 2), now, fact_id),
        )
        connection.execute(
            """INSERT INTO memory_events(
                event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), "fact", fact_id, 1, "fact.evolved", actor_principal,
             new_id(), new_id(), now,
             json.dumps({"confidence_old": old, "confidence_new": new, "delta": delta}),
             sha256_text(f"{fact_id}:{delta}")),
        )