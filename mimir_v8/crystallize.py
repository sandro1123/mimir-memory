"""Skill crystallization for Mímir v12 (M3b).

Borrowed from aiduMEI's skill-crystallization pattern:
- a daily worker clusters the last N days of facts by topic
- topics observed >= MIN_CRYSTAL_FREQ times become 'candidate' rows
- a suggestion (short synthetic summary) is stored on the candidate
- a human must explicitly approve before the skill takes effect; approval
  materializes a new pattern fact so the crystallized skill is queryable.

Design principle: candidates are never mutated in place. Every lifecycle step
(appear / approve / dismiss) writes a memory_event and, on approval, only ever
appends a new fact — no history is rewritten.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .store import CanonicalStore, new_id, sha256_text, utc_now


V17_ADDITIVE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS crystal_candidates (
        candidate_id TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        domain TEXT NOT NULL,
        freq INTEGER NOT NULL CHECK (freq >= 1),
        sample_ids TEXT NOT NULL,
        suggestion TEXT,
        reason TEXT,
        status TEXT NOT NULL CHECK (status IN ('candidate','approved','dismissed')),
        created_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT,
        crystal_fact_id TEXT
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_crystal_status
       ON crystal_candidates(status, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_crystal_topic
       ON crystal_candidates(topic, domain)""",
)


# Minimum number of observations before a topic becomes a candidate.
MIN_CRYSTAL_FREQ = 3
# How many days back a crystal scan looks for observations.
CRYSTAL_WINDOW_DAYS = 7
# A candidate is promoted only on explicit human approval.
CRYSTAL_STATUSES = frozenset({"candidate", "approved", "dismissed"})

# Small English stopword list used purely for topic extraction.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "not", "for", "of", "to", "in", "on",
    "with", "at", "by", "from", "is", "are", "was", "were", "this", "that",
    "these", "those", "it", "its", "we", "our", "you", "your", "i", "me", "my",
    "about", "into", "over", "after", "before", "have", "has", "had", "do",
    "does", "did", "be", "been", "being", "can", "could", "will", "would",
    "should", "shall", "may", "might", "must", "also", "very", "just", "only",
    "than", "then", "there", "when", "which", "who", "whom", "how", "so", "as",
    "use", "using", "used", "more", "most", "some", "any", "each", "every",
    "both", "one", "two", "all", "no", "yes", "etc", "e.g", "i.e",
}

_WORD_RE = re.compile(r"[a-z][a-z0-9]{2,}")


class CrystalError(RuntimeError):
    pass


def extract_topic(content: str) -> str:
    """Reduce a fact's content to a topical keyword key.

    Lowercases, drops stopwords, and keeps the five most frequent remaining
    tokens. Deterministic so the same content always maps to the same topic.
    """
    words = _WORD_RE.findall(content.lower())
    counts: dict[str, int] = {}
    for word in words:
        if word in _STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    top = sorted(counts, key=lambda w: (-counts[w], w))[:5]
    return " ".join(top)


def _bind_fact(fact: sqlite3.Row) -> dict[str, Any]:
    return {
        "fact_id": fact["fact_id"],
        "content": fact["content"],
        "summary": fact["summary"],
        "domain": fact["domain"],
    }


class CrystalService:
    """Cluster recent facts by topic and manage skill crystallization."""

    def __init__(self, store: CanonicalStore):
        self.store = store

    def scan(self, window_days: int = CRYSTAL_WINDOW_DAYS,
             min_freq: int = MIN_CRYSTAL_FREQ,
             actor_principal: str = "service:crystallize") -> dict:
        """Cluster active facts from the last window into topic candidates.

        Only clusters with >= min_freq observations become candidates. Rows
        already decided (approved/dismissed) are never resurrected. Existing
        open candidates are refreshed with the latest sample set.
        """
        if window_days < 1:
            raise ValueError("window_days must be >= 1")
        if min_freq < 2:
            raise ValueError("min_freq must be >= 2")
        now = utc_now()
        with self.store.transaction() as connection:
            rows = connection.execute(
                """SELECT fact_id, content, summary, domain
                FROM facts
                WHERE status='active'
                  AND recorded_at >= datetime('now', ?)""",
                (f"-{window_days} days",),
            ).fetchall()
            existing = {
                row["topic"]: row
                for row in connection.execute(
                    "SELECT candidate_id, topic, status FROM crystal_candidates"
                ).fetchall()
            }
            clusters = self._cluster(rows, min_freq)
            created = updated = skipped = 0
            for topic, members in clusters.items():
                member_ids = [m["fact_id"] for m in members]
                if topic in existing:
                    prior = existing[topic]
                    if prior["status"] != "candidate":
                        skipped += 1
                        continue
                    connection.execute(
                        """UPDATE crystal_candidates
                        SET freq=?, sample_ids=?, suggestion=?
                        WHERE candidate_id=?""",
                        (len(members), json.dumps(member_ids),
                         self._suggest(members), prior["candidate_id"]),
                    )
                    updated += 1
                    continue
                candidate_id = new_id()
                connection.execute(
                    """INSERT INTO crystal_candidates(
                        candidate_id, topic, domain, freq, sample_ids,
                        suggestion, status, created_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (candidate_id, topic, members[0]["domain"], len(members),
                     json.dumps(member_ids), self._suggest(members),
                     "candidate", now),
                )
                connection.execute(
                    """INSERT INTO memory_events(
                        event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                        actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id(), "crystal", candidate_id, 1, "crystal.appeared",
                     actor_principal, new_id(), new_id(), now,
                     json.dumps({"topic": topic, "freq": len(members),
                                 "sample_ids": member_ids}),
                     sha256_text(f"{candidate_id}:appeared:{now}")),
                )
                created += 1
        return {
            "status": "ok", "created": created, "updated": updated,
            "skipped": skipped, "window_days": window_days, "min_freq": min_freq,
        }

    def list(self, status: str = "candidate", limit: int = 50) -> list[dict]:
        if status not in CRYSTAL_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(CRYSTAL_STATUSES)}"
            )
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM crystal_candidates
                WHERE status=? ORDER BY freq DESC, created_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["sample_ids"] = json.loads(item["sample_ids"])
            result.append(item)
        return result

    def approve(self, candidate_id: str, actor_principal: str = "mentor") -> dict:
        """Human approval: materialize a crystallized skill as a pattern fact.

        Approval is the only step that can promote a candidate to effective.
        It creates a fresh pattern fact owned by the approver (appended —
        nothing existing is modified) and writes a crystal.approved event.
        """
        crystal_fact_id = None
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM crystal_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if not row:
                raise CrystalError(f"unknown crystal candidate: {candidate_id}")
            if row["status"] != "candidate":
                raise CrystalError(
                    f"candidate {candidate_id} is already {row['status']}"
                )
            crystal_fact_id = self._materialize(connection, row, actor_principal)
            now = utc_now()
            connection.execute(
                """UPDATE crystal_candidates
                SET status='approved', decided_at=?, decided_by=?,
                    crystal_fact_id=?
                WHERE candidate_id=?""",
                (now, actor_principal, crystal_fact_id, candidate_id),
            )
            connection.execute(
                """INSERT INTO memory_events(
                    event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                    actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), "crystal", candidate_id, 2, "crystal.approved",
                 actor_principal, new_id(), new_id(), now,
                 json.dumps({"topic": row["topic"], "crystal_fact_id": crystal_fact_id}),
                 sha256_text(f"{candidate_id}:approved:{crystal_fact_id}")),
            )
        return {
            "status": "ok", "candidate_id": candidate_id,
            "crystal_fact_id": crystal_fact_id,
        }

    def dismiss(self, candidate_id: str, reason: str = "",
                actor_principal: str = "mentor") -> dict:
        """Reject a candidate without materializing anything."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM crystal_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if not row:
                raise CrystalError(f"unknown crystal candidate: {candidate_id}")
            if row["status"] != "candidate":
                raise CrystalError(
                    f"candidate {candidate_id} is already {row['status']}"
                )
            now = utc_now()
            connection.execute(
                """UPDATE crystal_candidates
                SET status='dismissed', reason=?, decided_at=?, decided_by=?
                WHERE candidate_id=?""",
                (reason or None, now, actor_principal, candidate_id),
            )
            connection.execute(
                """INSERT INTO memory_events(
                    event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                    actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), "crystal", candidate_id, 2, "crystal.dismissed",
                 actor_principal, new_id(), new_id(), now,
                 json.dumps({"topic": row["topic"], "reason": reason}),
                 sha256_text(f"{candidate_id}:dismissed:{now}")),
            )
        return {"status": "ok", "candidate_id": candidate_id}

    # ── internals ────────────────────────────────────────────────────────────

    def _cluster(self, rows: list[sqlite3.Row], min_freq: int) -> dict[str, list[dict]]:
        """Group facts by extracted topic; keep only topics with >= min_freq."""
        groups: dict[str, list[dict]] = {}
        for row in rows:
            fact = _bind_fact(row)
            topic = extract_topic(fact["content"])
            if not topic:
                continue
            groups.setdefault(topic, []).append(fact)
        return {
            topic: members
            for topic, members in groups.items()
            if len(members) >= min_freq
        }

    def _suggest(self, members: list[dict]) -> str:
        """Deterministic suggestion: shared topic + most detailed member summary."""
        topic = extract_topic(members[0]["content"])
        source = max(
            (m["summary"] for m in members if m["summary"]),
            key=len,
            default="",
        )
        tag = f"[crystallized skill] {topic}"
        return f"{tag} :: {source[:400]}"

    def _materialize(self, connection: sqlite3.Connection, row: sqlite3.Row,
                     actor_principal: str) -> str:
        """Create the pattern fact backing an approved crystal."""
        from .schema import CreateFact

        content = (
            row["suggestion"] or f"Crystallized skill about {row['topic']}"
        )
        summary = (
            f"Crystallized skill from {row['freq']} observations "
            f"on topic '{row['topic']}'"
        )
        result = self.store.create_fact(
            CreateFact(
                content=content,
                owner_principal=actor_principal,
                domain=row["domain"],
                fact_type="pattern",
                summary=summary,
                confidence_score=0.9,
                human_status="confirmed",
            ),
            actor_principal,
            connection=connection,
        )
        return result["fact_id"]