"""Retention scheduling and fail-closed execution for Mímir v8.1."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .schema import TombstoneFact
from .store import CanonicalStore, ConflictError, NotFoundError, canonical_json, new_id, sha256_text, utc_now


@dataclass(frozen=True)
class RetentionSchedule:
    resource_type: str
    resource_id: str
    due_at: str
    reason: str
    legal_hold: bool = False
    idempotency_key: str = ""


class RetentionService:
    """Schedules retention actions and executes only due, non-held jobs."""

    def __init__(self, store: CanonicalStore):
        self.store = store

    def _referenced_by_committed_fact(self, resource_type: str, resource_id: str) -> bool:
        """True if a conversation message/source is cited by evidence of a committed candidate (now a fact).

        Such原文 is the provenance of durable knowledge; retention must not purge it.
        """
        with contextlib.closing(self.store.connect()) as connection:
            if resource_type == "conversation_message":
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM candidate_evidence ce "
                    "JOIN candidate_facts cf ON cf.candidate_id=ce.candidate_id "
                    "WHERE ce.message_id=? AND cf.status='committed'",
                    (resource_id,),
                ).fetchone()
            else:  # conversation_source: the source itself, or any of its messages, is cited
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM candidate_evidence ce "
                    "JOIN candidate_facts cf ON cf.candidate_id=ce.candidate_id "
                    "WHERE (ce.source_id=? OR ce.message_id IN "
                    "(SELECT message_id FROM conversation_messages WHERE source_id=?)) "
                    "AND cf.status='committed'",
                    (resource_id, resource_id),
                ).fetchone()
            return bool(row and row["n"] > 0)

    def schedule(self, command: RetentionSchedule, actor_principal: str) -> dict:
        allowed = {"conversation_source", "conversation_message", "candidate", "fact"}
        if command.resource_type not in allowed:
            raise ValueError("invalid retention resource_type")
        if not command.resource_id.strip() or not command.reason.strip() or not command.idempotency_key.strip():
            raise ValueError("resource_id, reason and idempotency_key are required")
        key = command.idempotency_key.strip()
        job_id = "retention-" + sha256_text(key)[:40]
        fingerprint = sha256_text(canonical_json({
            "resource_type": command.resource_type,
            "resource_id": command.resource_id,
            "due_at": command.due_at,
            "reason": command.reason.strip(),
            "legal_hold": command.legal_hold,
        }))
        now = utc_now()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT retention_job_id,status,event_seq,reason,resource_type,resource_id,due_at,legal_hold FROM retention_jobs WHERE retention_job_id=?",
                (job_id,),
            ).fetchone()
            if existing:
                existing_fingerprint = sha256_text(canonical_json({
                    "resource_type": existing["resource_type"],
                    "resource_id": existing["resource_id"],
                    "due_at": existing["due_at"],
                    "reason": existing["reason"],
                    "legal_hold": bool(existing["legal_hold"]),
                }))
                if existing_fingerprint != fingerprint:
                    raise ConflictError("retention idempotency key was reused with different content")
                return {"retention_job_id": existing["retention_job_id"], "status": existing["status"], "event_seq": existing["event_seq"], "idempotent_replay": True}
            if not self._exists(connection, command.resource_type, command.resource_id):
                raise NotFoundError(command.resource_id)
            status = "held" if command.legal_hold else "scheduled"
            event_id = new_id()
            payload = {"retention_job_id": job_id, "resource_type": command.resource_type, "resource_id": command.resource_id, "due_at": command.due_at, "legal_hold": command.legal_hold}
            payload_json = canonical_json(payload)
            event_seq = int(connection.execute(
                "INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, "retention", job_id, 1, "retention.held" if command.legal_hold else "retention.scheduled", actor_principal, event_id, event_id, now, payload_json, sha256_text(payload_json), command.idempotency_key),
            ).lastrowid)
            connection.execute(
                "INSERT INTO retention_jobs(retention_job_id,resource_type,resource_id,due_at,status,reason,legal_hold,requested_by,event_seq,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, command.resource_type, command.resource_id, command.due_at, status, command.reason.strip(), int(command.legal_hold), actor_principal, event_seq, now),
            )
        return {"retention_job_id": job_id, "status": status, "event_seq": event_seq, "idempotent_replay": False}

    def execute_due(self, actor_principal: str, *, limit: int = 50) -> dict:
        now = utc_now()
        executed, held = [], []
        with contextlib.closing(self.store.connect()) as connection:
            jobs = [dict(row) for row in connection.execute(
                "SELECT * FROM retention_jobs WHERE status='scheduled' AND due_at<=? ORDER BY due_at,retention_job_id LIMIT ?",
                (now, limit),
            ).fetchall()]
        for job in jobs:
            if job["legal_hold"]:
                with self.store.transaction() as connection:
                    connection.execute("UPDATE retention_jobs SET status='held' WHERE retention_job_id=? AND status='scheduled'", (job["retention_job_id"],))
                held.append(job["retention_job_id"])
                continue
            resource_type, resource_id = job["resource_type"], job["resource_id"]
            if resource_type in ("conversation_message", "conversation_source") and self._referenced_by_committed_fact(resource_type, resource_id):
                with self.store.transaction() as connection:
                    connection.execute("UPDATE retention_jobs SET status='held' WHERE retention_job_id=? AND status='scheduled'", (job["retention_job_id"],))
                    event_id = new_id()
                    payload = {"retention_job_id": job["retention_job_id"], "resource_type": resource_type, "resource_id": resource_id, "reason": "exempt: referenced by committed fact evidence"}
                    payload_json = canonical_json(payload)
                    connection.execute(
                        "INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (event_id, "retention", job["retention_job_id"], 3, "retention.exempted", actor_principal, event_id, event_id, now, payload_json, sha256_text(payload_json)),
                    )
                held.append(job["retention_job_id"])
                continue
            if resource_type == "fact":
                fact = self.store.get_fact(resource_id)
                if fact["status"] != "tombstoned":
                    self.store.tombstone_fact(
                        TombstoneFact(
                            fact_id=resource_id, expected_version=fact["current_version"],
                            reason=job["reason"], idempotency_key=f"retention-fact:{job['retention_job_id']}",
                        ), actor_principal=actor_principal,
                    )
            with self.store.transaction() as connection:
                current = connection.execute("SELECT status FROM retention_jobs WHERE retention_job_id=?", (job["retention_job_id"],)).fetchone()
                if not current or current["status"] != "scheduled":
                    continue
                purge_text = "[RETAINED CONTENT PURGED]"
                purge_hash = sha256_text(purge_text)
                if resource_type == "conversation_message":
                    connection.execute(
                        "UPDATE conversation_messages SET content_redacted=?,content_hash=?,metadata_json='{}' WHERE message_id=?",
                        (purge_text, purge_hash, resource_id),
                    )
                elif resource_type == "conversation_source":
                    connection.execute(
                        "UPDATE conversation_messages SET content_redacted=?,content_hash=?,metadata_json='{}' WHERE source_id=?",
                        (purge_text, purge_hash, resource_id),
                    )
                    connection.execute(
                        "UPDATE conversation_sources SET metadata_json='{}',source_uri=NULL,title='[SOURCE PURGED]',source_hash=? WHERE source_id=?",
                        (sha256_text(canonical_json({"source_id": resource_id, "status": "purged"})), resource_id),
                    )
                elif resource_type == "candidate":
                    connection.execute(
                        "UPDATE candidate_facts SET content='[CANDIDATE CONTENT PURGED]',summary='[CANDIDATE PURGED]',status='rejected',review_reason=?,reviewed_by=?,updated_at=? WHERE candidate_id=?",
                        (f"retention purge: {job['reason']}", actor_principal, now, resource_id),
                    )
                event_id = new_id()
                payload = {"retention_job_id": job["retention_job_id"], "resource_type": resource_type, "resource_id": resource_id}
                payload_json = canonical_json(payload)
                event_seq = int(connection.execute(
                    "INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, "retention", job["retention_job_id"], 2, "retention.executed", actor_principal, event_id, event_id, now, payload_json, sha256_text(payload_json)),
                ).lastrowid)
                connection.execute("UPDATE retention_jobs SET status='executed',executed_by=?,executed_at=?,event_seq=? WHERE retention_job_id=?", (actor_principal, now, event_seq, job["retention_job_id"]))
                executed.append(job["retention_job_id"])
        return {"executed": executed, "held": held, "count": len(executed)}

    def release_hold(self, retention_job_id: str, actor_principal: str, reason: str) -> dict:
        """Release a legal hold; only an explicit admin-boundary caller may invoke this."""
        if not retention_job_id.strip() or not reason.strip():
            raise ValueError("retention_job_id and reason are required")
        now = utc_now()
        with self.store.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM retention_jobs WHERE retention_job_id=?", (retention_job_id,)
            ).fetchone()
            if not job:
                raise NotFoundError(retention_job_id)
            if not job["legal_hold"] or job["status"] != "held":
                raise ConflictError("only an active legal hold can be released")
            payload = {"retention_job_id": retention_job_id, "reason": reason.strip()}
            payload_json = canonical_json(payload)
            event_id = new_id()
            event_seq = int(connection.execute(
                "INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, "retention", retention_job_id, 3, "retention.released", actor_principal, event_id, event_id, now, payload_json, sha256_text(payload_json)),
            ).lastrowid)
            connection.execute(
                "UPDATE retention_jobs SET status='scheduled', legal_hold=0, event_seq=? WHERE retention_job_id=?",
                (event_seq, retention_job_id),
            )
        return {"retention_job_id": retention_job_id, "status": "scheduled", "event_seq": event_seq}

    def cancel(self, retention_job_id: str, actor_principal: str, reason: str) -> dict:
        """Cancel a scheduled or held job without touching its resource."""
        if not retention_job_id.strip() or not reason.strip():
            raise ValueError("retention_job_id and reason are required")
        now = utc_now()
        with self.store.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM retention_jobs WHERE retention_job_id=?", (retention_job_id,)
            ).fetchone()
            if not job:
                raise NotFoundError(retention_job_id)
            if job["status"] not in {"scheduled", "held"}:
                raise ConflictError("only scheduled or held jobs can be cancelled")
            payload = {"retention_job_id": retention_job_id, "reason": reason.strip()}
            payload_json = canonical_json(payload)
            event_id = new_id()
            event_seq = int(connection.execute(
                "INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, "retention", retention_job_id, 3, "retention.cancelled", actor_principal, event_id, event_id, now, payload_json, sha256_text(payload_json)),
            ).lastrowid)
            connection.execute(
                "UPDATE retention_jobs SET status='cancelled', event_seq=? WHERE retention_job_id=?",
                (event_seq, retention_job_id),
            )
        return {"retention_job_id": retention_job_id, "status": "cancelled", "event_seq": event_seq}

    @staticmethod
    def _exists(connection, resource_type: str, resource_id: str) -> bool:
        table = {"conversation_source": "conversation_sources", "conversation_message": "conversation_messages", "candidate": "candidate_facts", "fact": "facts"}[resource_type]
        key = {"conversation_source": "source_id", "conversation_message": "message_id", "candidate": "candidate_id", "fact": "fact_id"}[resource_type]
        return connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (resource_id,)).fetchone() is not None
