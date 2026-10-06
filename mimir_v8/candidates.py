"""Candidate and review workflow for Mímir v8 learning governance."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from .dedup import check_duplicate
from .schema import CreateFact
from .store import CanonicalStore, ConflictError, NotFoundError, canonical_json, new_id, sha256_text, utc_now


logger = logging.getLogger(__name__)


class CandidatePolicyError(ValueError):
    """Raised when candidate governance policy rejects an operation."""


# ── XTMEM lineage strictness ladders (v12.2.0) ────────────────────────
# Higher value = stricter. Derived facts inherit max(source, proposal);
# the literals mirror schema.VISIBILITIES/SENSITIVITIES/EGRESS_POLICIES.
_VISIBILITY_STRICTNESS = {"all": 0, "shared": 1, "owner_only": 2}
_SENSITIVITY_STRICTNESS = {"internal": 0, "confidential": 1, "restricted": 2}
_EGRESS_STRICTNESS = {"external_allowed": 0, "redacted_external": 1, "local_only": 2}


def _stricter(proposed: str, source: str, ladder: dict) -> str:
    """Return the stricter of the two ACL tiers; never relax below the source."""
    return proposed if ladder[proposed] >= ladder[source] else source


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
                # 两处 return 同形态：重放不重跑判重（判重结果不入指纹，
                # 重跑不保证同值），键在但值恒为 None。
                "duplicate_hint": None,
            }
        if command.source_id:
            source = connection.execute(
                "SELECT content_hash FROM sources WHERE source_id=?", (command.source_id,)
            ).fetchone()
            if not source:
                raise CandidatePolicyError("source_id does not exist")
            if command.source_hash and source["content_hash"] != command.source_hash:
                raise CandidatePolicyError("source hash does not match canonical source")
        # ── XTMEM Lineage Gate (v12.2.0) ──────────────────────────────
        # Derived candidates (supersedes chain) inherit the strictest of
        # source vs proposal per ACL field; a dangling supersedes_fact_id is
        # Fail-Closed — rejected here with a governance error instead of
        # silently relaxing or surfacing a raw FK failure at insert time.
        if command.supersedes_fact_id:
            source_fact = connection.execute(
                """SELECT visibility, sensitivity, egress_policy FROM facts
                WHERE fact_id=?""", (command.supersedes_fact_id,)
            ).fetchone()
            if not source_fact:
                raise CandidatePolicyError(
                    "supersedes_fact_id does not exist (lineage fail-closed)"
                )
            validated_fact = replace(
                validated_fact,
                visibility=_stricter(validated_fact.visibility,
                                    source_fact["visibility"], _VISIBILITY_STRICTNESS),
                sensitivity=_stricter(validated_fact.sensitivity,
                                      source_fact["sensitivity"], _SENSITIVITY_STRICTNESS),
                egress_policy=_stricter(validated_fact.egress_policy,
                                         source_fact["egress_policy"], _EGRESS_STRICTNESS),
            )
        now = utc_now()
        candidate_id = new_id()
        event_id = new_id()
        # ③-4 判重接通（观察期）：标记疑似重复，不阻断、不改写。
        # 判重从未在生产验证过——直接阻断写入是拿生产做实验；异常必须
        # fail-open（判重是观察件，不是 lineage gate 那种安全件）。
        # 注意：判重结果绝不进下面的 payload（幂等指纹），否则同一
        # 幂等键的重放会因判重结果漂移而误报冲突。
        duplicate_hint = None
        try:
            # allow_full_scan=False：候选写入是热路径，LIKE '%probe%' 无索引，
            # 全表回退实测可到 ~290ms（3000 条重复语料）。观察期宁可漏报
            # （probe 落空即视为无重复）也不让生产写入等它。
            hint = check_duplicate(self.store, validated_fact.content,
                                   validated_fact.owner_principal,
                                   allow_full_scan=False)
            if hint.get("is_duplicate"):
                duplicate_hint = {
                    "match_type": hint["match_type"],
                    "similarity": hint["similarity"],
                    "matched_fact_id": hint["matched_fact_id"],
                }
        except Exception as exc:  # noqa: BLE001 - 观察件 fail-open，绝不阻断写入
            # 观测件静默失败 = 与「没有重复」不可区分，观察数据会失真——
            # 必须留 log 痕（但不阻断）。
            logger.warning("dedup check skipped for candidate write: %s: %s",
                           type(exc).__name__, exc)
            duplicate_hint = None
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
        if duplicate_hint:
            # 标记并入 uncertainty_json（追加一条 reason，不改既有内容）
            # + audit_log 留痕。观察期语义：只标记，候选照常创建。
            #
            # 整段裹在 SAVEPOINT 里：写入失败（如 hint 值不可序列化）只回滚
            # 标记本身，不把候选写入一起拖死——观察件 fail-open 的对象不只是
            # 「判重调用」，还包括「标记落库」这一步。
            try:
                connection.execute("SAVEPOINT dedup_marker")
                existing = list(command.uncertainty_reasons)
                existing.append(
                    f"possible_duplicate:{duplicate_hint['match_type']}:{duplicate_hint['similarity']}")
                connection.execute(
                    "UPDATE candidate_facts SET uncertainty_json=? WHERE candidate_id=?",
                    (canonical_json(existing), candidate_id),
                )
                connection.execute(
                    """INSERT INTO audit_log(
                        audit_id, occurred_at, actor_principal, action, resource_type,
                        resource_id, request_id, outcome, detail_json
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id(), now, actor_principal, "dedup.candidate_check",
                     "v10", candidate_id, new_id(), "success",
                     canonical_json({"candidate_id": candidate_id, **duplicate_hint})),
                )
                connection.execute("RELEASE dedup_marker")
            except Exception as exc:  # noqa: BLE001 - 标记失败不得拖死候选写入
                try:
                    connection.execute("ROLLBACK TO dedup_marker")
                    connection.execute("RELEASE dedup_marker")
                except Exception:  # noqa: BLE001 - SAVEPOINT 都没开成：无残留可回滚
                    pass
                logger.warning("dedup marker write failed (candidate kept): %s: %s",
                               type(exc).__name__, exc)
                duplicate_hint = None
        return {
            "candidate_id": candidate_id,
            "event_seq": event_seq,
            "status": "review_required",
            "idempotent_replay": False,
            "duplicate_hint": duplicate_hint,
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
        reviewer = str(candidate["reviewed_by"] or "")
        # audit 2026-09-07 P1-3: a human approval *is* the human review — 262
        # approved candidates used to land as unreviewed facts, losing the
        # signal. Only automatic (service:*) approvals leave the fact unreviewed.
        human_status = "confirmed" if reviewer and not reviewer.startswith("service:") else "unreviewed"
        result = self.store.create_fact(
            CreateFact(
                content=candidate["content"], summary=candidate["summary"],
                owner_principal=candidate["proposed_owner_principal"],
                domain=candidate["proposed_domain"], fact_type=candidate["proposed_fact_type"],
                visibility=candidate["proposed_visibility"],
                sensitivity=candidate["proposed_sensitivity"],
                egress_policy=candidate["proposed_egress_policy"],
                human_status=human_status,
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
                # v13.0 TKG: supersede opens a validity window on the new
                # relation and closes the reverse edge at the same instant
                # (valid_from == valid_until == now -> zero-width edge that
                # never shows as "currently valid", but history sees it).
                connection.execute(
                    """INSERT INTO relations(
                        relation_id, source_fact_id, target_type, target_id, relation_type,
                        status, created_by, created_at, source_event_id, valid_from, valid_until
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id(), result["fact_id"], "fact", superseded, "supersedes",
                     "active", actor_principal, now, event_id, now, ""),
                )
                connection.execute(
                    """INSERT INTO relations(
                        relation_id, source_fact_id, target_type, target_id, relation_type,
                        status, created_by, created_at, source_event_id, valid_from, valid_until
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id(), superseded, "fact", result["fact_id"], "superseded_by",
                     "active", actor_principal, now, event_id, now, now),
                )
        return {
            "candidate_id": candidate_id,
            "fact_id": result["fact_id"],
            "status": "committed",
            "idempotent_replay": result["idempotent_replay"],
            "committed_at": now,
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
