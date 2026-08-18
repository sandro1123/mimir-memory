"""Conflict resolution for Mímir v12 (M3a).

Borrowed from aiduMEI's conflict-resolution pattern:
- near-duplicate facts with materially different content are surfaced as conflicts
- a resolution picks a winner; the loser is marked disputed (never deleted)
- all resolution activity writes a memory_event so it is auditable and reversible.

Design principle: losing facts are never hard-deleted; they are flipped to
status 'disputed' so the record of the conflict survives.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .dedup import jaccard_similarity
from .store import CanonicalStore, new_id, sha256_text, utc_now


V16_ADDITIVE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS conflict_resolutions (
        conflict_id TEXT PRIMARY KEY,
        fact_id_a TEXT NOT NULL REFERENCES facts(fact_id),
        fact_id_b TEXT NOT NULL REFERENCES facts(fact_id),
        similarity REAL NOT NULL,
        conflict_type TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open','resolved','dismissed')),
        winner_fact_id TEXT,
        loser_fact_id TEXT,
        reason TEXT,
        resolved_by TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_conflict_status
       ON conflict_resolutions(status, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_conflict_fact_a
       ON conflict_resolutions(fact_id_a, status)""",
    """CREATE INDEX IF NOT EXISTS idx_conflict_fact_b
       ON conflict_resolutions(fact_id_b, status)""",
)


class ConflictResolutionError(RuntimeError):
    pass


class ConflictService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def detect(self, threshold: float = 0.6, actor_principal: str = "service:conflict") -> dict:
        """Scan active facts and surface near-duplicate contradiction pairs.

        A pair qualifies when both facts are active, they share a domain, are
        not identical content, and their Jaccard similarity is at or above the
        threshold. New pairs are inserted as 'open' conflicts; already open
        pairs are skipped.
        """
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0.0, 1.0]")
        with self.store.transaction() as connection:
            rows = connection.execute(
                """SELECT fact_id, domain, content, summary, confidence_score
                FROM facts WHERE status='active'"""
            ).fetchall()
        facts = [dict(r) for r in rows]
        created = 0
        existing = 0
        with self.store.transaction() as connection:
            seen = {
                (row["fact_id_a"], row["fact_id_b"]): row["status"]
                for row in connection.execute(
                    "SELECT fact_id_a, fact_id_b, status FROM conflict_resolutions"
                ).fetchall()
            }
            for i in range(len(facts)):
                for j in range(i + 1, len(facts)):
                    a, b = facts[i], facts[j]
                    if a["domain"] != b["domain"]:
                        continue
                    if a["fact_id"] == b["fact_id"]:
                        continue
                    if a["content"].strip() == b["content"].strip():
                        continue
                    sim = jaccard_similarity(a["content"], b["content"])
                    if sim < threshold:
                        continue
                    key = (a["fact_id"], b["fact_id"])
                    if key in seen:
                        existing += 1
                        continue
                    conflict_id = new_id()
                    now = utc_now()
                    connection.execute(
                        """INSERT INTO conflict_resolutions(
                            conflict_id, fact_id_a, fact_id_b, similarity,
                            conflict_type, status, created_at
                        ) VALUES(?,?,?,?,?,?,?)""",
                        (conflict_id, a["fact_id"], b["fact_id"], round(sim, 4),
                         "near_duplicate_contradiction", "open", now),
                    )
                    connection.execute(
                        """INSERT INTO memory_events(
                            event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                            actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (new_id(), "conflict", conflict_id, 1, "conflict.detected",
                         actor_principal, new_id(), new_id(), now,
                         json.dumps({"fact_id_a": a["fact_id"], "fact_id_b": b["fact_id"],
                                     "similarity": round(sim, 4)}),
                         sha256_text(f"{a['fact_id']}:{b['fact_id']}:{round(sim, 4)}")),
                    )
                    created += 1
        return {"status": "ok", "created": created, "existing": existing}

    def list(self, status: str = "open", limit: int = 50) -> list[dict]:
        if status not in ("open", "resolved", "dismissed"):
            raise ValueError("status must be open|resolved|dismissed")
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM conflict_resolutions
                WHERE status=? ORDER BY created_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["facts"] = {}
            for side in ("a", "b"):
                fid = row[f"fact_id_{side}"]
                fact = self.store.get_fact(fid)
                item["facts"][side] = {
                    "fact_id": fid,
                    "content": fact["content"],
                    "summary": fact["summary"],
                    "confidence_score": fact["confidence_score"],
                    "status": fact["status"],
                }
            result.append(item)
        return result

    def resolve(self, conflict_id: str, winner_fact_id: str, reason: str = "",
                actor_principal: str = "service:conflict") -> dict:
        """Resolve an open conflict: winner stays active, loser becomes disputed.

        The loser is never deleted — status flips to 'disputed' and a
        fact.conflict_lost event is written for auditability.
        """
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM conflict_resolutions WHERE conflict_id=?",
                (conflict_id,),
            ).fetchone()
            if not row:
                raise ConflictResolutionError(f"unknown conflict: {conflict_id}")
            if row["status"] != "open":
                raise ConflictResolutionError(f"conflict {conflict_id} is already {row['status']}")
            sides = {row["fact_id_a"], row["fact_id_b"]}
            if winner_fact_id not in sides:
                raise ConflictResolutionError(
                    f"winner must be one of the conflict facts: {sorted(sides)}"
                )
            loser_fact_id = (sides - {winner_fact_id}).pop()
            winner = self.store.get_fact(winner_fact_id)
            if winner["status"] != "active":
                raise ConflictResolutionError(f"winner fact {winner_fact_id} is not active")
            now = utc_now()
            # flip loser to disputed, preserving history via a version bump + event
            loser = connection.execute(
                "SELECT * FROM facts WHERE fact_id=?", (loser_fact_id,)
            ).fetchone()
            if loser["status"] == "active":
                self._mark_disputed(connection, loser, reason, actor_principal, now)
            connection.execute(
                """UPDATE conflict_resolutions
                SET status='resolved', winner_fact_id=?, loser_fact_id=?,
                    reason=?, resolved_by=?, resolved_at=?
                WHERE conflict_id=?""",
                (winner_fact_id, loser_fact_id, reason or None, actor_principal, now,
                 conflict_id),
            )
            connection.execute(
                """INSERT INTO memory_events(
                    event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                    actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), "conflict", conflict_id, 2, "conflict.resolved",
                 actor_principal, new_id(), new_id(), now,
                 json.dumps({"winner_fact_id": winner_fact_id,
                             "loser_fact_id": loser_fact_id, "reason": reason}),
                 sha256_text(f"{conflict_id}:{winner_fact_id}:{loser_fact_id}")),
            )
        return {
            "status": "ok", "conflict_id": conflict_id,
            "winner_fact_id": winner_fact_id, "loser_fact_id": loser_fact_id,
        }

    def dismiss(self, conflict_id: str, reason: str = "",
                actor_principal: str = "service:conflict") -> dict:
        """Close a conflict without changing fact status."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM conflict_resolutions WHERE conflict_id=?",
                (conflict_id,),
            ).fetchone()
            if not row:
                raise ConflictResolutionError(f"unknown conflict: {conflict_id}")
            if row["status"] != "open":
                raise ConflictResolutionError(f"conflict {conflict_id} is already {row['status']}")
            now = utc_now()
            connection.execute(
                """UPDATE conflict_resolutions
                SET status='dismissed', reason=?, resolved_by=?, resolved_at=?
                WHERE conflict_id=?""",
                (reason or None, actor_principal, now, conflict_id),
            )
        return {"status": "ok", "conflict_id": conflict_id}

    def _mark_disputed(self, connection, loser, reason: str,
                       actor_principal: str, now: str) -> None:
        new_version = int(loser["current_version"]) + 1
        connection.execute(
            "UPDATE facts SET status='disputed', current_version=?, updated_at=? WHERE fact_id=?",
            (new_version, now, loser["fact_id"]),
        )
        connection.execute(
            """INSERT INTO fact_versions(
                fact_id, version, content_hash, snapshot_json, change_type,
                change_reason, actor_principal, source_event_id, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (loser["fact_id"], new_version, loser["content_hash"],
             json.dumps({"status": "disputed", "reason": reason,
                         "previous_status": loser["status"]}),
             "dispute", reason, actor_principal, new_id(), now),
        )
        connection.execute(
            """INSERT INTO memory_events(
                event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), "fact", loser["fact_id"], new_version, "fact.conflict_lost",
             actor_principal, new_id(), new_id(), now,
             json.dumps({"reason": reason, "previous_status": loser["status"]}),
             sha256_text(f"{loser['fact_id']}:conflict_lost:{now}")),
        )
