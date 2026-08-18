"""Opinion and Observation services for Mímir v10 — idempotent, transactional."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .store import CanonicalStore, new_id, sha256_text, utc_now
from .schema import OPINION_CONFIDENCE_DELTA, OPINION_STALE_DAYS

# Minimum distinct opinion owners before a topic may consolidate into an observation.
MIN_DISTINCT_OWNERS = 2


class OpinionService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def set_opinion(self, fact_id: str, topic: str, stance: str,
                    confidence: float, owner_principal: str,
                    evidence_id: str | None = None, actor_principal: str = "mentor") -> dict:
        """Atomically upsert opinion + write audit event via store.transaction."""
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT opinion_id, confidence, evidence_ids FROM opinions WHERE fact_id=? AND owner_principal=?",
                (fact_id, owner_principal),
            ).fetchone()
            evidence_ids = json.loads(existing["evidence_ids"]) if existing else []
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)

            now = utc_now()
            event_id = new_id()
            payload = json.dumps({
                "fact_id": fact_id, "topic": topic, "stance": stance,
                "confidence": confidence, "evidence_ids": evidence_ids,
                "updated": bool(existing),
            })

            if existing:
                conn.execute(
                    "UPDATE opinions SET stance=?, confidence=?, evidence_ids=?, updated_at=? WHERE opinion_id=?",
                    (stance, confidence, json.dumps(evidence_ids), now, existing["opinion_id"]),
                )
                opinion_id = existing["opinion_id"]
            else:
                opinion_id = new_id()
                conn.execute(
                    "INSERT INTO opinions(opinion_id, fact_id, topic, stance, confidence, evidence_ids, owner_principal, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (opinion_id, fact_id, topic, stance, confidence, json.dumps(evidence_ids), owner_principal, now, now),
                )

            conn.execute(
                """INSERT INTO memory_events(
                    event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                    actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, "opinion", opinion_id, 1,
                    "opinion.set", actor_principal,
                    new_id(), new_id(), now, payload,
                    sha256_text(f"{fact_id}:{topic}:{stance}"),
                ),
            )
            return {"opinion_id": opinion_id, "fact_id": fact_id, "updated": bool(existing)}

    def evolve_confidence(self, fact_id: str, owner_principal: str,
                          signal: str) -> dict:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM opinions WHERE fact_id=? AND owner_principal=?",
                (fact_id, owner_principal),
            ).fetchone()
            if not row:
                return {"error": "opinion not found"}
            delta = OPINION_CONFIDENCE_DELTA if signal in ("confirm", "useful") else -OPINION_CONFIDENCE_DELTA
            new_conf = max(0.0, min(1.0, row["confidence"] + delta))
            conn.execute(
                "UPDATE opinions SET confidence=?, updated_at=? WHERE opinion_id=?",
                (new_conf, utc_now(), row["opinion_id"]),
            )
            return {"opinion_id": row["opinion_id"], "new_confidence": new_conf}

    def get_opinions_for_facts(self, fact_ids: list[str]) -> list[dict]:
        if not fact_ids:
            return []
        placeholders = ",".join("?" for _ in fact_ids)
        with self.store.transaction() as conn:
            rows = conn.execute(
                f"SELECT * FROM opinions WHERE fact_id IN ({placeholders}) ORDER BY updated_at DESC",
                fact_ids,
            ).fetchall()
            return [dict(r) for r in rows]

    def consolidate_observations(self, owner_principal: str = "mentor") -> dict:
        with self.store.transaction() as conn:
            opinions = conn.execute(
                "SELECT * FROM opinions WHERE confidence >= 0.6 ORDER BY updated_at DESC"
            ).fetchall()
            if not opinions:
                return {"created": 0, "message": "no opinions to consolidate"}
            by_type: dict[str, list[dict]] = {}
            for o in opinions:
                t = o["topic"]
                if t not in by_type:
                    by_type[t] = []
                by_type[t].append(dict(o))
            created = 0
            for topic, group in by_type.items():
                owners = {o["owner_principal"] for o in group}
                if len(group) < 3 or len(owners) < MIN_DISTINCT_OWNERS:
                    continue
                avg_conf = sum(o["confidence"] for o in group) / len(group)
                opinion_ids = [o["opinion_id"] for o in group]
                existing = conn.execute(
                    "SELECT observation_id FROM observations WHERE summary=? AND owner_principal=?",
                    (topic, owner_principal),
                ).fetchone()
                now = utc_now()
                if existing:
                    obs_id = existing["observation_id"]
                    conn.execute(
                        "UPDATE observations SET confidence=?, supporting_opinion_ids=?, updated_at=? WHERE observation_id=?",
                        (round(avg_conf, 2), json.dumps(opinion_ids), now, obs_id),
                    )
                else:
                    obs_id = new_id()
                    conn.execute(
                        "INSERT INTO observations(observation_id,summary,supporting_opinion_ids,confidence,stale,owner_principal,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (obs_id, topic, json.dumps(opinion_ids), round(avg_conf, 2), 0, owner_principal, now, now),
                    )
                conn.execute(
                    """INSERT INTO memory_events(
                        event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                        actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id(), "observation", obs_id, 1, "observation.consolidated",
                     owner_principal, new_id(), new_id(), now,
                     json.dumps({"topic": topic, "opinion_count": len(group),
                                 "distinct_owners": len(owners), "confidence": round(avg_conf, 2)}),
                     sha256_text(f"observation:{obs_id}:{now}")),
                )
                created += 1
            return {"created": created, "total_opinions": len(opinions)}

    def get_observations(self, owner_principal: str | None = None) -> list[dict]:
        with self.store.transaction() as conn:
            if owner_principal:
                rows = conn.execute(
                    "SELECT * FROM observations WHERE owner_principal=? AND stale=0 ORDER BY confidence DESC",
                    (owner_principal,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM observations WHERE stale=0 ORDER BY confidence DESC"
                ).fetchall()
            return [dict(r) for r in rows]