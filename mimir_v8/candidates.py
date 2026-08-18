"""Candidate and review workflow for Mímir v8 learning governance."""

from __future__ import annotations

from dataclasses import dataclass

from .schema import CreateFact
from .store import CanonicalStore, ConflictError, NotFoundError, canonical_json, new_id, sha256_text, utc_now


class CandidatePolicyError(ValueError):
    """Raised when candidate governance policy rejects an operation."""


@dataclass(frozen=True)
class CreateCandidate:
    content: str
    proposed_owner_principal: str
    proposed_domain: str
    proposed_fact_type: str
    summary: str | None = None
    proposed_visibility: str = "owner_only"
    proposed_sensitivity: str = "internal"
    proposed_egress_policy: str = "local_only"
    source_id: str | None = None
    source_hash: str | None = None
    confidence_score: float | None = None
    uncertainty_reasons: tuple[str, ...] = ()
    supersedes_fact_id: str | None = None
    idempotency_key: str = ""
    idempotency_fingerprint: str | None = None
    extraction_id: str | None = None


@dataclass(frozen=True)
class ReviewCandidate:
    candidate_id: str
    action: str
    reason: str
    idempotency_key: str


class CandidateService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def create_candidate(self, command: CreateCandidate, actor_principal: str) -> dict:
        """Create a candidate in its own transaction for standalone callers."""
        with self.store.transaction() as connection:
            return self.create_candidate_in_transaction(connection, command, actor_principal)

    def create_candidate_in_transaction(self, connection, command: CreateCandidate, actor_principal: str) -> dict:
        """Create a candidate using the caller's transaction and connection.

        This is intentionally the only lower-level candidate creation entry point.
        Callers that compose candidate creation with evidence and ingestion state
        must pass their existing connection so every write shares one rollback.
        """
        validated_fact = CreateFact(
            content=command.content,
            summary=command.summary,
            owner_principal=command.proposed_owner_principal,
            domain=command.proposed_domain,
            fact_type=command.proposed_fact_type,
            visibility=command.proposed_visibility,
            sensitivity=command.proposed_sensitivity,
            egress_policy=command.proposed_egress_policy,
            confidence_score=command.confidence_score,
        ).validated()
        key = command.idempotency_key.strip()
        if not key:
            raise CandidatePolicyError("idempotency_key is required")
        fingerprint = command.idempotency_fingerprint or sha256_text(
            canonical_json(
                {
                    "content": validated_fact.content,
                    "summary": validated_fact.summary,
                    "owner": validated_fact.owner_principal,
                    "domain": validated_fact.domain,
                    "fact_type": validated_fact.fact_type,
                    "visibility": validated_fact.visibility,
                    "sensitivity": validated_fact.sensitivity,
                    "egress_policy": validated_fact.egress_policy,
                    "source_id": command.source_id,
                    "source_hash": command.source_hash,
                    "confidence_score": validated_fact.confidence_score,
                    "uncertainty_reasons": command.uncertainty_reasons,
                    "supersedes_fact_id": command.supersedes_fact_id,
                }
            )
        )
        replay = connection.execute(
            "SELECT * FROM memory_events WHERE idempotency_key=?", (key,)
        ).fetchone()
        if replay:
            payload = __import__("json").loads(replay["payload_json"])
            if payload.get("request_fingerprint") != fingerprint:
                raise ConflictError("candidate idempotency key was reused with different content")
            return {
                "candidate_id": replay["aggregate_id"],
                "event_seq": replay["event_seq"],
                "status": "review_required",
                "idempotent_replay": True,
                "extraction_id": payload.get("extraction_id"),
            }
        if command.source_id:
            source = connection.execute(
                "SELECT content_hash FROM sources WHERE source_id=?", (command.source_id,)
            ).fetchone()
            if not source:
                raise CandidatePolicyError("source_id does not exist")
            if command.source_hash and source["content_hash"] != command.source_hash:
                raise CandidatePolicyError("source hash does not match canonical source")
        now = utc_now()
        candidate_id = new_id()
        event_id = new_id()
        payload = {
            "candidate_id": candidate_id,
            "status": "review_required",
            "content_hash": sha256_text(validated_fact.content),
            "source_id": command.source_id,
            "request_fingerprint": fingerprint,
            "extraction_id": command.extraction_id,
        }
        event_seq = self._insert_event(
            connection, event_id, candidate_id, 1, "candidate.created",
            actor_principal, key, now, payload,
        )
        connection.execute(
            """INSERT INTO candidate_facts(
                candidate_id, status, content, summary, proposed_owner_principal,
                proposed_domain, proposed_fact_type, proposed_visibility,
                proposed_sensitivity, proposed_egress_policy, source_id, source_hash,
                confidence_score, uncertainty_json, proposed_by, supersedes_fact_id,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id, "review_required", validated_fact.content,
                validated_fact.summary, validated_fact.owner_principal,
                validated_fact.domain, validated_fact.fact_type,
                validated_fact.visibility, validated_fact.sensitivity,
                validated_fact.egress_policy, command.source_id, command.source_hash,
                validated_fact.confidence_score,
                canonical_json(list(command.uncertainty_reasons)), actor_principal,
                command.supersedes_fact_id, now, now,
            ),
        )
        return {
            "candidate_id": candidate_id,
            "event_seq": event_seq,
            "status": "review_required",
            "idempotent_replay": False,
        }

    def review_candidate(self, command: ReviewCandidate, reviewer_principal: str) -> dict:
        if command.action not in {"approve", "reject", "needs_more_evidence"}:
            raise CandidatePolicyError(f"invalid review action: {command.action}")
        reason = command.reason.strip()
        key = command.idempotency_key.strip()
        if not reason or not key:
            raise CandidatePolicyError("review reason and idempotency_key are required")
        fingerprint = sha256_text(
            canonical_json(
                {"candidate_id": command.candidate_id, "action": command.action, "reason": reason}
            )
        )
        now = utc_now()
        with self.store.transaction() as connection:
            replay = connection.execute(
                "SELECT * FROM memory_events WHERE idempotency_key=?", (key,)
            ).fetchone()
            if replay:
                payload = __import__("json").loads(replay["payload_json"])
                if payload.get("request_fingerprint") != fingerprint:
                    raise ConflictError("review idempotency key was reused with different content")
                candidate = connection.execute(
                    "SELECT status, committed_fact_id FROM candidate_facts WHERE candidate_id=?",
                    (command.candidate_id,),
                ).fetchone()
                return {
                    "candidate_id": command.candidate_id,
                    "status": candidate["status"],
                    "fact_id": candidate["committed_fact_id"],
                    "event_seq": replay["event_seq"],
                    "idempotent_replay": True,
                }
            candidate = connection.execute(
                "SELECT * FROM candidate_facts WHERE candidate_id=?", (command.candidate_id,)
            ).fetchone()
            if not candidate:
                raise NotFoundError(command.candidate_id)
            if candidate["status"] not in ("review_required", "provisional", "human_review", "needs_more_evidence"):
                raise ConflictError(f"candidate is not reviewable: {candidate['status']}")
            status = {
                "approve": "approved",
                "reject": "rejected",
                "needs_more_evidence": "needs_more_evidence",
            }[command.action]
            event_type = {
                "approve": "candidate.approved",
                "reject": "candidate.rejected",
                "needs_more_evidence": "candidate.needs_more_evidence",
            }[command.action]
            event_id = new_id()
            payload = {
                "candidate_id": command.candidate_id,
                "action": command.action,
                "status": status,
                "reason_hash": sha256_text(reason),
                "request_fingerprint": fingerprint,
            }
            event_seq = self._insert_event(
                connection, event_id, command.candidate_id, 2, event_type,
                reviewer_principal, key, now, payload,
            )
            connection.execute(
                """UPDATE candidate_facts SET status=?, reviewed_by=?, review_reason=?,
                updated_at=? WHERE candidate_id=?""",
                (status, reviewer_principal, reason, now, command.candidate_id),
            )
            connection.execute(
                """INSERT INTO review_actions(
                    review_id, candidate_id, action, reason, reviewer_principal,
                    event_seq, created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (new_id(), command.candidate_id, command.action, reason, reviewer_principal, event_seq, now),
            )
        return {
            "candidate_id": command.candidate_id,
            "status": status,
            "fact_id": None,
            "event_seq": event_seq,
            "idempotent_replay": False,
        }

    def commit_approved(self, candidate_id: str, actor_principal: str, idempotency_key: str) -> dict:
        with self.store.transaction() as connection:
            candidate = connection.execute(
                "SELECT * FROM candidate_facts WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not candidate:
                raise NotFoundError(candidate_id)
            if candidate["status"] == "committed":
                return {
                    "candidate_id": candidate_id,
                    "fact_id": candidate["committed_fact_id"],
                    "status": "committed",
                    "idempotent_replay": True,
                }
            if candidate["status"] != "approved":
                raise ConflictError("only approved candidates can be committed")
        result = self.store.create_fact(
            CreateFact(
                content=candidate["content"], summary=candidate["summary"],
                owner_principal=candidate["proposed_owner_principal"],
                domain=candidate["proposed_domain"], fact_type=candidate["proposed_fact_type"],
                visibility=candidate["proposed_visibility"],
                sensitivity=candidate["proposed_sensitivity"],
                egress_policy=candidate["proposed_egress_policy"],
                confidence_score=candidate["confidence_score"],
                source_kind="candidate", source_uri=f"mimir-v8://candidate/{candidate_id}",
                source_hash=candidate["source_hash"], idempotency_key=idempotency_key,
            ),
            actor_principal=actor_principal,
        )
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT status, committed_fact_id FROM candidate_facts WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if current["status"] == "committed":
                return {
                    "candidate_id": candidate_id,
                    "fact_id": current["committed_fact_id"],
                    "status": "committed",
                    "idempotent_replay": True,
                }
            now = utc_now()
            event_id = new_id()
            payload = {
                "candidate_id": candidate_id,
                "fact_id": result["fact_id"],
                "fact_event_id": result["event_id"],
                "supersedes_fact_id": candidate["supersedes_fact_id"],
            }
            self._insert_event(
                connection, event_id, candidate_id, 3, "candidate.committed",
                actor_principal, f"candidate-committed:{candidate_id}:{result['fact_id']}", now, payload,
            )
            connection.execute(
                """UPDATE candidate_facts SET status='committed', committed_fact_id=?,
                updated_at=? WHERE candidate_id=? AND status='approved'""",
                (result["fact_id"], now, candidate_id),
            )
            superseded = candidate["supersedes_fact_id"]
            if superseded:
                connection.execute(
                    """INSERT INTO relations(
                        relation_id, source_fact_id, target_type, target_id, relation_type,
                        status, created_by, created_at, source_event_id
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id(), result["fact_id"], "fact", superseded, "supersedes", "active", actor_principal, now, event_id),
                )
        return {
            "candidate_id": candidate_id,
            "fact_id": result["fact_id"],
            "status": "committed",
            "idempotent_replay": result["idempotent_replay"],
        }

    @staticmethod
    def _insert_event(connection, event_id, aggregate_id, version, event_type, actor, key, now, payload):
        payload_json = canonical_json(payload)
        cursor = connection.execute(
            """INSERT INTO memory_events(
                event_id, aggregate_type, aggregate_id, aggregate_version,
                event_type, actor_principal, request_id, correlation_id,
                occurred_at, payload_json, payload_hash, idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, "candidate", aggregate_id, version, event_type, actor,
                event_id, event_id, now, payload_json, sha256_text(payload_json), key,
            ),
        )
        return int(cursor.lastrowid)
