"""Governed extraction and evidence attachment for Mimir v8.1.

Extraction is deliberately composed inside one SQLite transaction. The public
service remains the only source-category gate, while the transaction-aware
candidate/evidence helpers allow API, worker, and direct callers to share the
same rollback boundary.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Callable

from .candidates import CandidateService, CreateCandidate
from .learning import redact_text
from .schema import ValidationError
from .store import CanonicalStore, ConflictError, NotFoundError, canonical_json, new_id, sha256_text, utc_now


@dataclass(frozen=True)
class EvidenceInput:
    source_id: str
    message_id: str
    quote_text: str
    start_offset: int | None = None
    end_offset: int | None = None


class ExtractionService:
    """Create evidence-backed Candidates; never write canonical facts."""

    def __init__(self, store: CanonicalStore):
        self.store = store
        self.candidates = CandidateService(store)

    def extract_candidate(
        self,
        *,
        run_id: str,
        source_id: str,
        actor_principal: str,
        content: str,
        owner_principal: str,
        domain: str,
        fact_type: str,
        idempotency_key: str,
        summary: str | None = None,
        evidence: tuple[EvidenceInput, ...] = (),
        policy_version: str = "v8.1-default",
        failure_hook: Callable[[str], None] | None = None,
    ) -> dict:
        """Perform the complete extraction mutation in one rollback boundary.

        ``failure_hook`` exists only for isolated tests. It is never supplied by
        production API or worker callers. Any exception raised after BEGIN
        causes store.transaction() to roll back every write in this method.
        """
        redacted = redact_text(content)
        now = utc_now()
        extraction_id = new_id()
        self._validate_evidence_shapes(evidence)
        request_fingerprint = self._request_fingerprint(
            run_id=run_id,
            source_id=source_id,
            actor_principal=actor_principal,
            content=redacted.text,
            owner_principal=owner_principal,
            domain=domain,
            fact_type=fact_type,
            summary=summary,
            evidence=evidence,
            policy_version=policy_version,
        )

        with self.store.transaction() as connection:
            main_source_id = self._validate_main_source(connection, run_id, source_id)
            self._validate_all_evidence(connection, main_source_id, evidence)

            self._failure(failure_hook, "after_evidence_preflight")

            candidate = self.candidates.create_candidate_in_transaction(
                connection,
                CreateCandidate(
                    content=redacted.text,
                    summary=summary,
                    proposed_owner_principal=owner_principal,
                    proposed_domain=domain,
                    proposed_fact_type=fact_type,
                    source_id=source_id,
                    source_hash=self._source_hash_in_connection(connection, source_id),
                    uncertainty_reasons=("automatic_extraction_requires_review",),
                    idempotency_key=idempotency_key,
                    idempotency_fingerprint=request_fingerprint,
                    extraction_id=extraction_id,
                ),
                actor_principal,
            )
            self._failure(failure_hook, "after_candidate_write")

            # A replay of a previously completed extraction is returned without
            # creating a duplicate extraction run or evidence rows.
            if candidate["idempotent_replay"]:
                existing = self._existing_completed_extraction(
                    connection,
                    extraction_id=candidate.get("extraction_id"),
                    run_id=run_id,
                    candidate_id=candidate["candidate_id"],
                )
                if existing is not None:
                    return existing
                raise ConflictError(
                    "idempotency replay has no matching completed extraction"
                )

            connection.execute(
                """INSERT INTO extraction_runs(
                    extraction_id,run_id,extractor_principal,policy_version,status,
                    candidate_count,started_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (extraction_id, run_id, actor_principal, policy_version, "started", 0, now),
            )
            self._failure(failure_hook, "after_extraction_insert")

            attached = []
            for index, item in enumerate(evidence, start=1):
                self._failure(failure_hook, f"before_evidence_{index}")
                attached.append(
                    self._attach_evidence_in_transaction(
                        connection=connection,
                        candidate_id=candidate["candidate_id"],
                        evidence=item,
                        actor_principal=actor_principal,
                    )
                )
                self._failure(failure_hook, f"after_evidence_{index}")

            self._failure(failure_hook, "before_completion")
            completed = utc_now()
            connection.execute(
                """UPDATE extraction_runs
                   SET status='completed', candidate_count=?, completed_at=?
                   WHERE extraction_id=?""",
                (1, completed, extraction_id),
            )
            connection.execute(
                "UPDATE ingestion_runs SET status='extracted' WHERE run_id=? AND status='stored'",
                (run_id,),
            )
            self._failure(failure_hook, "after_completion")

            return {
                "extraction_id": extraction_id,
                "candidate": candidate,
                "evidence": attached,
                "status": "completed",
            }

    @staticmethod
    def _request_fingerprint(
        *,
        run_id: str,
        source_id: str,
        actor_principal: str,
        content: str,
        owner_principal: str,
        domain: str,
        fact_type: str,
        summary: str | None,
        evidence: tuple[EvidenceInput, ...],
        policy_version: str,
    ) -> str:
        return sha256_text(canonical_json({
            "run_id": run_id,
            "source_id": source_id,
            "actor_principal": actor_principal,
            "content": content,
            "owner_principal": owner_principal,
            "domain": domain,
            "fact_type": fact_type,
            "summary": summary,
            "policy_version": policy_version,
            "evidence": [
                {
                    "source_id": item.source_id,
                    "message_id": item.message_id,
                    "quote_text": redact_text(item.quote_text).text,
                    "start_offset": item.start_offset,
                    "end_offset": item.end_offset,
                }
                for item in evidence
            ],
        }))

    @staticmethod
    def _failure(failure_hook: Callable[[str], None] | None, point: str) -> None:
        if failure_hook is not None:
            failure_hook(point)

    @staticmethod
    def _validate_evidence_shapes(evidence: tuple[EvidenceInput, ...]) -> None:
        for item in evidence:
            if not item.quote_text.strip():
                raise ValidationError("evidence quote_text is required")
            if item.start_offset is not None and item.start_offset < 0:
                raise ValidationError("evidence start_offset must be non-negative")
            if item.end_offset is not None and item.end_offset < 0:
                raise ValidationError("evidence end_offset must be non-negative")
            if item.start_offset is not None and item.end_offset is not None and item.end_offset < item.start_offset:
                raise ValidationError("evidence end_offset must not precede start_offset")

    @staticmethod
    def _validate_main_source(connection, run_id: str, source_id: str) -> str:
        row = connection.execute(
            """SELECT r.run_id, r.source_id, s.source_category
               FROM ingestion_runs r
               JOIN conversation_sources s ON s.source_id = r.source_id
               WHERE r.run_id = ?""",
            (run_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(run_id)
        if str(row["source_id"]) != source_id:
            raise ValidationError(
                f"run_id {run_id} source_id mismatch: expected {row['source_id']}, got {source_id}"
            )
        category = row["source_category"]
        if category is None:
            raise ValidationError(f"source_category is NULL for run_id {run_id}, cannot extract")
        if category != "conversation":
            raise ValidationError(
                f"source_category must be 'conversation' for extraction, got '{category}' for run_id {run_id}"
            )
        return str(row["source_id"])

    @staticmethod
    def _validate_all_evidence(connection, main_source_id: str, evidence: tuple[EvidenceInput, ...]) -> None:
        for item in evidence:
            source = connection.execute(
                "SELECT source_id, source_category FROM conversation_sources WHERE source_id=?",
                (item.source_id,),
            ).fetchone()
            if not source:
                raise ValidationError(f"evidence source_id {item.source_id} does not exist")
            if str(source["source_id"]) != main_source_id:
                raise ValidationError(
                    f"evidence source_id {item.source_id} does not match main source_id {main_source_id}"
                )
            if source["source_category"] != "conversation":
                raise ValidationError(
                    f"evidence source {item.source_id} has source_category '{source['source_category']}', expected 'conversation'"
                )
            message = connection.execute(
                "SELECT message_id FROM conversation_messages WHERE message_id=? AND source_id=?",
                (item.message_id, item.source_id),
            ).fetchone()
            if not message:
                raise ValidationError(
                    f"evidence message_id {item.message_id} not found in source {item.source_id}"
                )

    @staticmethod
    def _source_hash_in_connection(connection, source_id: str) -> str:
        row = connection.execute(
            "SELECT content_hash FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(source_id)
        return row[0]

    @staticmethod
    def _existing_completed_extraction(
        connection,
        *,
        extraction_id: str | None,
        run_id: str,
        candidate_id: str,
    ) -> dict | None:
        if extraction_id:
            row = connection.execute(
                """SELECT extraction_id, candidate_count
                   FROM extraction_runs
                   WHERE extraction_id=? AND run_id=? AND status='completed'""",
                (extraction_id, run_id),
            ).fetchone()
        else:
            rows = connection.execute(
                """SELECT extraction_id, candidate_count
                   FROM extraction_runs
                   WHERE run_id=? AND status='completed'
                   ORDER BY completed_at, extraction_id""",
                (run_id,),
            ).fetchall()
            if len(rows) > 1:
                raise ConflictError(
                    "legacy idempotency replay is ambiguous for a run with multiple completed extractions"
                )
            row = rows[0] if rows else None
        if not row:
            return None
        candidate = connection.execute(
            "SELECT * FROM candidate_facts WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if not candidate:
            return None
        evidence_rows = connection.execute(
            """SELECT evidence_id, candidate_id, source_id, message_id,
                      quote_text_redacted, start_offset, end_offset,
                      evidence_hash, added_by, created_at
               FROM candidate_evidence WHERE candidate_id=?
               ORDER BY created_at, evidence_id""",
            (candidate_id,),
        ).fetchall()
        return {
            "extraction_id": row["extraction_id"],
            "candidate": {
                "candidate_id": candidate["candidate_id"],
                "status": candidate["status"],
                "idempotent_replay": True,
            },
            "evidence": [dict(item) for item in evidence_rows],
            "status": "completed",
            "idempotent_replay": True,
        }

    @staticmethod
    def _attach_evidence_in_transaction(connection, candidate_id: str, evidence: EvidenceInput, actor_principal: str) -> dict:
        result = redact_text(evidence.quote_text)
        evidence_hash = sha256_text(canonical_json({
            "source_id": evidence.source_id,
            "message_id": evidence.message_id,
            "quote_text": result.text,
            "start_offset": evidence.start_offset,
            "end_offset": evidence.end_offset,
        }))
        existing = connection.execute(
            "SELECT evidence_id FROM candidate_evidence WHERE candidate_id=? AND evidence_hash=?",
            (candidate_id, evidence_hash),
        ).fetchone()
        if existing:
            event = connection.execute(
                """SELECT event_seq FROM memory_events
                   WHERE event_type='evidence.attached' AND aggregate_id=?
                     AND json_extract(payload_json, '$.evidence_id')=?
                   ORDER BY event_seq DESC LIMIT 1""",
                (candidate_id, existing["evidence_id"]),
            ).fetchone()
            return {
                "evidence_id": existing["evidence_id"],
                "event_seq": event["event_seq"] if event else None,
                "idempotent_replay": True,
            }

        now = utc_now()
        event_id = new_id()
        evidence_id = new_id()
        payload = {
            "candidate_id": candidate_id,
            "evidence_id": evidence_id,
            "source_id": evidence.source_id,
            "message_id": evidence.message_id,
            "evidence_hash": evidence_hash,
            "redaction_applied": result.redacted,
        }
        payload_json = canonical_json(payload)
        cursor = connection.execute(
            """INSERT INTO memory_events(
                event_id, aggregate_type, aggregate_id, aggregate_version,
                event_type, actor_principal, request_id, correlation_id,
                occurred_at, payload_json, payload_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, "candidate", candidate_id, 4, "evidence.attached",
                actor_principal, event_id, event_id, now, payload_json,
                sha256_text(payload_json),
            ),
        )
        event_seq = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO candidate_evidence(
                evidence_id, candidate_id, source_id, message_id,
                quote_text_redacted, start_offset, end_offset, evidence_hash,
                added_by, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id, candidate_id, evidence.source_id, evidence.message_id,
                result.text, evidence.start_offset, evidence.end_offset,
                evidence_hash, actor_principal, now,
            ),
        )
        return {
            "evidence_id": evidence_id,
            "event_seq": event_seq,
            "redaction_applied": result.redacted,
            "idempotent_replay": False,
        }

    def _source_hash(self, source_id: str) -> str:
        with contextlib.closing(self.store.connect()) as connection:
            return self._source_hash_in_connection(connection, source_id)
