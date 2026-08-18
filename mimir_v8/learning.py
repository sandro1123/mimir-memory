"""Mímir v8.1 conversation ingestion and explicit memory boundary.

This module deliberately stores only DLP-redacted conversation content. Automatic
inputs stop at Candidate; only an authorized reviewer can commit canonical facts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from .candidates import CandidateService, CreateCandidate
from .classifier import classify
from .schema import (
    AGENT_IDS,
    CONVERSATION_ROLES,
    DOMAINS,
    FACT_TYPES,
    MEMORY_MODES,
    RETENTION_CLASSES,
    ValidationError,
)
from .store import CanonicalStore, ConflictError, canonical_json, new_id, sha256_text, utc_now


_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(api[_ -]?key|token|secret|password|passwd)\s*[:=]\s*[^\s,;]+"), r"\1=[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"), "[PRIVATE KEY REDACTED]"),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}"), "sk-[REDACTED]"),
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redacted: bool
    rules: tuple[str, ...]


def redact_text(value: str) -> RedactionResult:
    if not isinstance(value, str):
        raise ValidationError("content must be text")
    text = value
    rules: list[str] = []
    for index, (pattern, replacement) in enumerate(_SECRET_PATTERNS, 1):
        updated, count = pattern.subn(replacement, text)
        if count:
            rules.append(f"secret_rule_{index}")
            text = updated
    return RedactionResult(text=text, redacted=bool(rules), rules=tuple(rules))


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str
    principal_id: str | None = None
    created_at: str | None = None
    metadata: dict = field(default_factory=dict)

    def validated(self) -> "ConversationMessage":
        if self.role not in CONVERSATION_ROLES:
            raise ValidationError(f"invalid conversation role: {self.role}")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValidationError("conversation message content must be non-empty")
        if len(self.content) > 200_000:
            raise ValidationError("conversation message exceeds 200000 characters")
        return ConversationMessage(
            role=self.role,
            content=self.content,
            principal_id=self.principal_id.strip() if self.principal_id else None,
            created_at=self.created_at,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class ConversationEnvelope:
    connector_type: str
    connector_id: str
    session_id: str | None
    owner_principal: str
    memory_mode: str
    retention_class: str
    messages: tuple[ConversationMessage, ...]
    source_uri: str | None = None
    title: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    metadata: dict = field(default_factory=dict)
    idempotency_key: str = ""

    def validated(self) -> "ConversationEnvelope":
        if self.connector_type not in {"hermes_cdc", "external_agent", "workbuddy", "file", "rss", "web", "document"}:
            if not self.connector_type.strip():
                raise ValidationError("connector_type is required")
        if not self.connector_id.strip():
            raise ValidationError("connector_id is required")
        if not self.owner_principal.strip():
            raise ValidationError("owner_principal is required")
        if self.memory_mode not in MEMORY_MODES:
            raise ValidationError(f"invalid memory_mode: {self.memory_mode}")
        if self.retention_class not in RETENTION_CLASSES:
            raise ValidationError(f"invalid retention_class: {self.retention_class}")
        if not self.messages or len(self.messages) > 1000:
            raise ValidationError("messages must contain between 1 and 1000 items")
        key = self.idempotency_key.strip()
        if not key:
            raise ValidationError("idempotency_key is required")
        return ConversationEnvelope(
            connector_type=self.connector_type,
            connector_id=self.connector_id.strip(),
            session_id=self.session_id.strip() if self.session_id else None,
            owner_principal=self.owner_principal.strip(),
            memory_mode=self.memory_mode,
            retention_class=self.retention_class,
            messages=tuple(message.validated() for message in self.messages),
            source_uri=self.source_uri.strip() if self.source_uri else None,
            title=self.title.strip() if self.title else None,
            started_at=self.started_at,
            ended_at=self.ended_at,
            metadata=dict(self.metadata),
            idempotency_key=key,
        )


class LearningService:
    """Owns the v8.1 ingestion boundary and explicit memory commands."""

    def __init__(self, store: CanonicalStore):
        self.store = store
        self.candidates = CandidateService(store)

    def ingest_conversation(self, envelope: ConversationEnvelope, actor_principal: str) -> dict:
        env = envelope.validated()
        redacted = [redact_text(message.content) for message in env.messages]
        fingerprint = sha256_text(canonical_json({
            "connector_type": env.connector_type,
            "connector_id": env.connector_id,
            "session_id": env.session_id,
            "owner_principal": env.owner_principal,
            "memory_mode": env.memory_mode,
            "retention_class": env.retention_class,
            "messages": [
                {"role": m.role, "content": r.text, "principal_id": m.principal_id}
                for m, r in zip(env.messages, redacted)
            ],
        }))
        now = utc_now()
        with self.store.transaction() as connection:
            replay = connection.execute(
                "SELECT run_id, source_id, request_fingerprint, status, message_count, redacted_count FROM ingestion_runs WHERE idempotency_key=?",
                (env.idempotency_key,),
            ).fetchone()
            if replay:
                if replay["request_fingerprint"] != fingerprint:
                    raise ConflictError("ingestion idempotency key was reused with different content")
                source_category = classify(env.connector_type)
                return {"run_id": replay["run_id"], "source_id": replay["source_id"], "status": replay["status"], "message_count": replay["message_count"], "redacted_count": replay["redacted_count"], "source_category": source_category, "idempotent_replay": True}

            source_id = new_id()
            source_hash = sha256_text(canonical_json({"fingerprint": fingerprint, "connector_id": env.connector_id}))
            source_category = classify(env.connector_type)
            connection.execute(
                "INSERT INTO conversation_sources(source_id,connector_type,connector_id,session_id,source_uri,source_hash,title,owner_principal,retention_class,memory_mode,source_category,started_at,ended_at,ingested_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, env.connector_type, env.connector_id, env.session_id, env.source_uri, source_hash, env.title, env.owner_principal, env.retention_class, env.memory_mode, source_category, env.started_at, env.ended_at, now, canonical_json(env.metadata)),
            )
            connection.execute(
                "INSERT INTO sources(source_id,source_kind,source_uri,content_hash,title,retrieved_at,license,trust_tier) VALUES(?,?,?,?,?,?,?,?)",
                (source_id, "conversation", env.source_uri, source_hash, env.title, now, "user-provided", "conversation"),
            )
            for ordinal, (message, result) in enumerate(zip(env.messages, redacted)):
                connection.execute(
                    "INSERT INTO conversation_messages(message_id,source_id,ordinal,role,principal_id,content_redacted,content_hash,redaction_applied,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id(), source_id, ordinal, message.role, message.principal_id, result.text, sha256_text(result.text), int(result.redacted), message.created_at or now, canonical_json({"redaction_rules": result.rules, **message.metadata})),
                )
            run_id = new_id()
            redacted_count = sum(1 for item in redacted if item.redacted)
            connection.execute(
                "INSERT INTO ingestion_runs(run_id,source_id,status,requested_by,idempotency_key,request_fingerprint,message_count,redacted_count,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, source_id, "stored", actor_principal, env.idempotency_key, fingerprint, len(env.messages), redacted_count, now, now),
            )
            self._event(connection, "conversation", source_id, "conversation.source_registered", actor_principal, now, {"source_id": source_id, "run_id": run_id, "message_count": len(env.messages), "redacted_count": redacted_count})
            self._event(connection, "conversation", source_id, "conversation.ingested", actor_principal, now, {"source_id": source_id, "run_id": run_id, "memory_mode": env.memory_mode})
        return {"run_id": run_id, "source_id": source_id, "status": "stored", "message_count": len(env.messages), "redacted_count": redacted_count, "source_category": source_category, "idempotent_replay": False}

    def remember(self, content: str, owner_principal: str, domain: str, fact_type: str, actor_principal: str, idempotency_key: str, summary: str | None = None, retention_class: str = "standard") -> dict:
        if owner_principal not in AGENT_IDS:
            raise ValidationError("owner_principal must be a registered agent")
        if domain not in DOMAINS or fact_type not in FACT_TYPES:
            raise ValidationError("invalid domain or fact_type")
        if retention_class not in RETENTION_CLASSES:
            raise ValidationError(f"invalid retention_class: {retention_class}")
        result = redact_text(content)
        source_id = self._create_explicit_source(result.text, owner_principal, actor_principal, idempotency_key)
        candidate = self.candidates.create_candidate(
            CreateCandidate(content=result.text, summary=summary, proposed_owner_principal=owner_principal, proposed_domain=domain, proposed_fact_type=fact_type, source_id=source_id, source_hash=self._source_hash(source_id), proposed_visibility="owner_only", proposed_egress_policy="local_only", uncertainty_reasons=("explicit_memory_requires_review",), idempotency_key=f"{idempotency_key}:candidate"),
            actor_principal,
        )
        return {**candidate, "source_id": source_id, "redaction_applied": result.redacted, "redaction_rules": result.rules, "retention_class": retention_class}

    def forget(self, fact_id: str, expected_version: int, reason: str, actor_principal: str, idempotency_key: str) -> dict:
        fact = self.store.get_fact(fact_id)
        if fact["owner_principal"] != actor_principal:
            raise PermissionError("only the fact owner can forget a fact")
        from .schema import TombstoneFact
        return self.store.tombstone_fact(
            TombstoneFact(fact_id=fact_id, expected_version=expected_version, reason=reason, idempotency_key=idempotency_key),
            actor_principal=actor_principal,
        )

    def correct(self, fact_id: str, expected_version: int, corrected_content: str, summary: str | None, reason: str, actor_principal: str, idempotency_key: str) -> dict:
        fact = self.store.get_fact(fact_id)
        if fact["owner_principal"] != actor_principal:
            raise PermissionError("only the fact owner can correct a fact")
        if int(fact["current_version"]) != int(expected_version):
            raise ConflictError(
                f"fact version conflict: expected {expected_version}, current {fact['current_version']}"
            )
        result = redact_text(corrected_content)
        candidate = self.candidates.create_candidate(
            CreateCandidate(
                content=result.text,
                summary=summary,
                proposed_owner_principal=fact["owner_principal"],
                proposed_domain=fact["domain"],
                proposed_fact_type=fact["fact_type"],
                proposed_visibility=fact["visibility"],
                proposed_sensitivity=fact["sensitivity"],
                proposed_egress_policy=fact["egress_policy"],
                confidence_score=fact["confidence_score"],
                uncertainty_reasons=("correction_requires_review", reason),
                supersedes_fact_id=fact_id,
                idempotency_key=f"{idempotency_key}:candidate",
            ),
            actor_principal,
        )
        return {**candidate, "corrects_fact_id": fact_id, "redaction_applied": result.redacted, "redaction_rules": result.rules}

    def submit_feedback(
        self,
        feedback_type: str,
        feedback_text: str,
        submitted_by: str,
        idempotency_key: str,
        *,
        candidate_id: str | None = None,
        fact_id: str | None = None,
    ) -> dict:
        allowed = {"useful", "incorrect", "stale", "duplicate", "harmful", "withdraw"}
        if feedback_type not in allowed:
            raise ValidationError(f"invalid feedback_type: {feedback_type}")
        if not isinstance(feedback_text, str) or not feedback_text.strip():
            raise ValidationError("feedback_text is required")
        key = idempotency_key.strip()
        if not key:
            raise ValidationError("idempotency_key is required")
        if bool(candidate_id) == bool(fact_id):
            raise ValidationError("exactly one of candidate_id or fact_id is required")
        redacted = redact_text(feedback_text)
        fingerprint = sha256_text(canonical_json({
            "candidate_id": candidate_id,
            "fact_id": fact_id,
            "feedback_type": feedback_type,
            "feedback_text": redacted.text,
        }))
        now = utc_now()
        with self.store.transaction() as connection:
            replay = connection.execute(
                "SELECT feedback_id,event_seq FROM learning_feedback WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if replay:
                event = connection.execute(
                    "SELECT payload_json FROM memory_events WHERE event_seq=?", (replay["event_seq"],)
                ).fetchone()
                payload = json.loads(event["payload_json"]) if event else {}
                if payload.get("request_fingerprint") != fingerprint:
                    raise ConflictError("feedback idempotency key was reused with different content")
                return {
                    "feedback_id": replay["feedback_id"],
                    "event_seq": replay["event_seq"],
                    "status": "submitted",
                    "redaction_applied": redacted.redacted,
                    "redaction_rules": redacted.rules,
                    "idempotent_replay": True,
                }
            if candidate_id:
                target = connection.execute(
                    "SELECT candidate_id FROM candidate_facts WHERE candidate_id=?", (candidate_id,)
                ).fetchone()
            else:
                target = connection.execute(
                    "SELECT fact_id FROM facts WHERE fact_id=?", (fact_id,)
                ).fetchone()
            if not target:
                raise ValidationError("feedback target does not exist")
            feedback_id = new_id()
            event_id = new_id()
            payload = {
                "feedback_id": feedback_id,
                "candidate_id": candidate_id,
                "fact_id": fact_id,
                "feedback_type": feedback_type,
                "feedback_hash": sha256_text(redacted.text),
                "redaction_applied": redacted.redacted,
                "request_fingerprint": fingerprint,
            }
            event_seq = self._insert_feedback_event(
                connection, event_id, candidate_id or fact_id, submitted_by, key, now, payload
            )
            connection.execute(
                "INSERT INTO learning_feedback(feedback_id,candidate_id,fact_id,feedback_type,feedback_text,submitted_by,idempotency_key,event_seq,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (feedback_id, candidate_id, fact_id, feedback_type, redacted.text, submitted_by, key, event_seq, now),
            )
        return {
            "feedback_id": feedback_id,
            "event_seq": event_seq,
            "status": "submitted",
            "redaction_applied": redacted.redacted,
            "redaction_rules": redacted.rules,
            "idempotent_replay": False,
        }

    def _create_explicit_source(self, content: str, owner: str, actor: str, key: str) -> str:
        source_id = "explicit-" + sha256_text(key)[:40]
        now = utc_now()
        content_hash = sha256_text(content)
        with self.store.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO sources(source_id,source_kind,source_uri,content_hash,title,retrieved_at,license,trust_tier) VALUES(?,?,?,?,?,?,?,?)", (source_id, "explicit_memory", f"mimir-v8.1://remember/{key}", content_hash, "Explicit memory", now, "user-provided", "explicit"))
        return source_id

    def _source_hash(self, source_id: str) -> str:
        import contextlib
        with contextlib.closing(self.store.connect()) as connection:
            row = connection.execute("SELECT content_hash FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not row:
                raise ValidationError("explicit source was not created")
            return row[0]

    @staticmethod
    def _insert_feedback_event(connection, event_id, aggregate_id, actor, key, now, payload):
        payload_json = canonical_json(payload)
        cursor = connection.execute(
            "INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "learning_feedback", aggregate_id, 1, "learning.feedback_submitted", actor, event_id, event_id, now, payload_json, sha256_text(payload_json), key),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _event(connection, aggregate_type, aggregate_id, event_type, actor, now, payload):
        event_id = new_id()
        payload_json = canonical_json(payload)
        cursor = connection.execute("INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (event_id, aggregate_type, aggregate_id, 1, event_type, actor, event_id, event_id, now, payload_json, sha256_text(payload_json)))
        return int(cursor.lastrowid)
