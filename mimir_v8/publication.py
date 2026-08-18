"""Guarded Obsidian publication workflow for Mímir v8.

Mímir owns Agent facts. Obsidian owns human cognition. This module never scans a
Vault and never treats a whole Markdown document as a disposable projection. It
only replaces explicitly named managed sections after revision and hash checks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .schema import (
    DOCUMENT_STATUSES,
    DOCUMENT_TARGET_SCOPES,
    DOCUMENT_TYPES,
    VAULT_ADAPTER_PRINCIPAL,
)
from .store import CanonicalStore, ConflictError, canonical_json, new_id, sha256_text, utc_now

_ALLOWED_ROOTS = {"00-系统", "10-项目", "20-领域", "30-知识", "50-运维", "90-机器收件箱"}
_FORBIDDEN_ROOTS = {"60-敏感信息"}
_SECTION_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_START = "<!-- mimir:managed:start name={name} -->"
_END = "<!-- mimir:managed:end name={name} -->"


class PublicationPolicyError(ValueError):
    """Raised when a publication would violate the human/Agent boundary."""


@dataclass(frozen=True)
class RegisterDocument:
    vault_path: str
    document_type: str
    owner_principal: str
    target_scope: str = "private"
    status: str = "active"
    document_id: str | None = None


@dataclass(frozen=True)
class RequestPublication:
    document_id: str
    expected_revision: int
    expected_previous_hash: str | None
    managed_sections: dict[str, str]
    fact_ids: tuple[str, ...] = ()
    idempotency_key: str = ""


class PublicationService:
    """Coordinates publication records without performing filesystem I/O."""

    def __init__(self, store: CanonicalStore):
        self.store = store

    def register_document(self, command: RegisterDocument, current_text: str) -> dict:
        path = self._validate_path(command.vault_path)
        if command.document_type not in DOCUMENT_TYPES:
            raise PublicationPolicyError(f"invalid document_type: {command.document_type}")
        if command.status not in DOCUMENT_STATUSES:
            raise PublicationPolicyError(f"invalid document status: {command.status}")
        if command.target_scope not in DOCUMENT_TARGET_SCOPES:
            raise PublicationPolicyError(f"invalid target_scope: {command.target_scope}")
        owner = command.owner_principal.strip()
        if not owner:
            raise PublicationPolicyError("owner_principal is required")
        document_id = command.document_id or new_id()
        now = utc_now()
        human_hash = human_content_hash(current_text)
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM documents WHERE vault_path=? OR document_id=?",
                (path, document_id),
            ).fetchone()
            if existing:
                if existing["vault_path"] != path or existing["document_id"] != document_id:
                    raise ConflictError("document identity conflicts with an existing registration")
                return {**dict(existing), "idempotent_replay": True}
            connection.execute(
                """INSERT INTO documents(
                    document_id, vault_path, document_type, status, owner_principal,
                    target_scope, current_revision, publication_hash, human_hash,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    path,
                    command.document_type,
                    command.status,
                    owner,
                    command.target_scope,
                    0,
                    None,
                    human_hash,
                    now,
                    now,
                ),
            )
        return {
            "document_id": document_id,
            "vault_path": path,
            "current_revision": 0,
            "publication_hash": None,
            "human_hash": human_hash,
            "idempotent_replay": False,
        }

    def request_publication(self, command: RequestPublication, actor_principal: str) -> dict:
        self._require_vault_adapter(actor_principal)
        if command.expected_revision < 0:
            raise PublicationPolicyError("expected_revision cannot be negative")
        if not command.idempotency_key.strip():
            raise PublicationPolicyError("idempotency_key is required")
        sections = validate_managed_sections(command.managed_sections)
        fact_ids = tuple(dict.fromkeys(fact_id.strip() for fact_id in command.fact_ids if fact_id.strip()))
        desired_hash = publication_hash(sections, fact_ids)
        fingerprint = sha256_text(
            canonical_json(
                {
                    "document_id": command.document_id,
                    "expected_revision": command.expected_revision,
                    "expected_previous_hash": command.expected_previous_hash,
                    "managed_sections": sections,
                    "fact_ids": fact_ids,
                }
            )
        )
        now = utc_now()
        with self.store.transaction() as connection:
            replay = connection.execute(
                "SELECT * FROM document_publications WHERE idempotency_key=?",
                (command.idempotency_key.strip(),),
            ).fetchone()
            if replay:
                if replay["request_fingerprint"] != fingerprint:
                    raise ConflictError("publication idempotency key was reused with different content")
                return {**dict(replay), "idempotent_replay": True}

            document = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (command.document_id,)
            ).fetchone()
            if not document:
                raise PublicationPolicyError("document is not registered")
            if document["status"] == "archived":
                raise PublicationPolicyError("archived documents cannot be published")
            if int(document["current_revision"]) != command.expected_revision:
                raise ConflictError("document revision changed before publication request")
            if document["publication_hash"] != command.expected_previous_hash:
                raise ConflictError("document publication hash changed before request")
            open_request = connection.execute(
                """SELECT publication_id FROM document_publications
                WHERE document_id=? AND requested_revision=? AND status='requested'""",
                (command.document_id, command.expected_revision + 1),
            ).fetchone()
            if open_request:
                raise ConflictError("another publication request is already open for this revision")

            facts = []
            for fact_id in fact_ids:
                fact = connection.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
                if not fact or fact["status"] != "active":
                    raise PublicationPolicyError(f"fact is unavailable for publication: {fact_id}")
                self._enforce_fact_policy(dict(fact), document["target_scope"])
                facts.append(dict(fact))

            publication_id = new_id()
            event_id = new_id()
            request_payload = {
                "document_id": command.document_id,
                "publication_id": publication_id,
                "requested_revision": command.expected_revision + 1,
                "desired_hash": desired_hash,
                "managed_section_names": sorted(sections),
                "fact_ids": fact_ids,
                "request_fingerprint": fingerprint,
            }
            event_seq = self._insert_document_event(
                connection,
                event_id=event_id,
                document_id=command.document_id,
                revision=command.expected_revision + 1,
                event_type="document.publication_requested",
                actor_principal=actor_principal,
                occurred_at=now,
                payload=request_payload,
            )
            connection.execute(
                """INSERT INTO document_publications(
                    publication_id, document_id, requested_revision,
                    expected_previous_hash, desired_hash, managed_sections_json,
                    status, requested_by, request_event_seq, idempotency_key,
                    request_fingerprint, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    publication_id,
                    command.document_id,
                    command.expected_revision + 1,
                    command.expected_previous_hash,
                    desired_hash,
                    canonical_json(sections),
                    "requested",
                    actor_principal,
                    event_seq,
                    command.idempotency_key.strip(),
                    fingerprint,
                    now,
                ),
            )
            for fact in facts:
                connection.execute(
                    """INSERT INTO document_fact_references(
                        document_id, fact_id, publication_id, created_at
                    ) VALUES(?,?,?,?)""",
                    (command.document_id, fact["fact_id"], publication_id, now),
                )
        return {
            "publication_id": publication_id,
            "document_id": command.document_id,
            "requested_revision": command.expected_revision + 1,
            "desired_hash": desired_hash,
            "status": "requested",
            "event_seq": event_seq,
            "idempotent_replay": False,
        }

    def apply_publication(
        self,
        publication_id: str,
        current_text: str,
        *,
        observed_revision: int,
        observed_publication_hash: str | None,
        actor_principal: str,
    ) -> dict:
        self._require_vault_adapter(actor_principal)
        with self.store.transaction() as connection:
            publication = connection.execute(
                "SELECT * FROM document_publications WHERE publication_id=?", (publication_id,)
            ).fetchone()
            if not publication:
                raise PublicationPolicyError("publication request does not exist")
            document = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (publication["document_id"],)
            ).fetchone()
            if publication["status"] == "published":
                return {
                    "publication_id": publication_id,
                    "document_id": publication["document_id"],
                    "status": "published",
                    "rendered_text": current_text,
                    "idempotent_replay": True,
                }
            if publication["status"] == "conflict":
                raise ConflictError("publication is in the conflict queue")

            conflict_type = None
            if int(document["current_revision"]) != observed_revision:
                conflict_type = "revision_mismatch"
            elif publication["expected_previous_hash"] != observed_publication_hash:
                conflict_type = "publication_hash_mismatch"
            elif document["human_hash"] != human_content_hash(current_text):
                conflict_type = "human_content_changed"
            if conflict_type:
                conflict = self._record_conflict(
                    connection,
                    publication=dict(publication),
                    document=dict(document),
                    conflict_type=conflict_type,
                    observed_revision=observed_revision,
                    observed_hash=observed_publication_hash,
                    actor_principal=actor_principal,
                )
                return {**conflict, "rendered_text": current_text, "idempotent_replay": False}

            try:
                rendered = replace_managed_sections(
                    current_text, json.loads(publication["managed_sections_json"])
                )
            except PublicationPolicyError:
                conflict = self._record_conflict(
                    connection,
                    publication=dict(publication),
                    document=dict(document),
                    conflict_type="managed_section_structure_invalid",
                    observed_revision=observed_revision,
                    observed_hash=observed_publication_hash,
                    actor_principal=actor_principal,
                )
                return {**conflict, "rendered_text": current_text, "idempotent_replay": False}

            new_human_hash = human_content_hash(rendered)
            if new_human_hash != document["human_hash"]:
                raise PublicationPolicyError("managed-section rendering changed human-authored content")
            now = utc_now()
            event_id = new_id()
            payload = {
                "document_id": document["document_id"],
                "publication_id": publication_id,
                "revision": publication["requested_revision"],
                "publication_hash": publication["desired_hash"],
            }
            event_seq = self._insert_document_event(
                connection,
                event_id=event_id,
                document_id=document["document_id"],
                revision=publication["requested_revision"],
                event_type="document.published",
                actor_principal=actor_principal,
                occurred_at=now,
                payload=payload,
            )
            connection.execute(
                """UPDATE document_publications SET status='published',
                published_event_seq=?, completed_at=? WHERE publication_id=?""",
                (event_seq, now, publication_id),
            )
            connection.execute(
                """UPDATE documents SET current_revision=?, publication_hash=?,
                human_hash=?, updated_at=? WHERE document_id=?""",
                (
                    publication["requested_revision"],
                    publication["desired_hash"],
                    new_human_hash,
                    now,
                    document["document_id"],
                ),
            )
        return {
            "publication_id": publication_id,
            "document_id": document["document_id"],
            "status": "published",
            "revision": publication["requested_revision"],
            "publication_hash": publication["desired_hash"],
            "event_seq": event_seq,
            "rendered_text": rendered,
            "idempotent_replay": False,
        }

    def submit_feedback(
        self,
        document_id: str,
        current_text: str,
        feedback: str,
        *,
        actor_principal: str,
    ) -> dict:
        feedback = feedback.strip()
        if not feedback:
            raise PublicationPolicyError("feedback is required")
        if len(feedback) > 10_000:
            raise PublicationPolicyError("feedback exceeds 10000 characters")
        now = utc_now()
        feedback_id = new_id()
        feedback_hash = sha256_text(feedback)
        current_human_hash = human_content_hash(current_text)
        with self.store.transaction() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if not document:
                raise PublicationPolicyError("document is not registered")
            event_id = new_id()
            payload = {
                "document_id": document_id,
                "revision": document["current_revision"],
                "document_human_hash": current_human_hash,
                "feedback_id": feedback_id,
                "feedback_hash": feedback_hash,
            }
            event_seq = self._insert_document_event(
                connection,
                event_id=event_id,
                document_id=document_id,
                revision=document["current_revision"],
                event_type="document.feedback_submitted",
                actor_principal=actor_principal,
                occurred_at=now,
                payload=payload,
            )
            connection.execute(
                """INSERT INTO document_feedback(
                    feedback_id, document_id, document_revision, human_hash,
                    feedback_text, feedback_hash, submitted_by,
                    submitted_event_seq, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    feedback_id,
                    document_id,
                    document["current_revision"],
                    current_human_hash,
                    feedback,
                    feedback_hash,
                    actor_principal,
                    event_seq,
                    now,
                ),
            )
        return {
            "document_id": document_id,
            "feedback_id": feedback_id,
            "event_seq": event_seq,
            "status": "submitted",
        }

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        resolution: str,
        actor_principal: str,
    ) -> dict:
        resolution = resolution.strip()
        if not resolution:
            raise PublicationPolicyError("conflict resolution is required")
        now = utc_now()
        with self.store.transaction() as connection:
            conflict = connection.execute(
                "SELECT * FROM document_conflicts WHERE conflict_id=?", (conflict_id,)
            ).fetchone()
            if not conflict:
                raise PublicationPolicyError("conflict does not exist")
            if conflict["status"] == "resolved":
                return {"conflict_id": conflict_id, "status": "resolved", "idempotent_replay": True}
            event_id = new_id()
            payload = {
                "document_id": conflict["document_id"],
                "publication_id": conflict["publication_id"],
                "conflict_id": conflict_id,
                "resolution": resolution,
            }
            document = connection.execute(
                "SELECT current_revision FROM documents WHERE document_id=?",
                (conflict["document_id"],),
            ).fetchone()
            event_seq = self._insert_document_event(
                connection,
                event_id=event_id,
                document_id=conflict["document_id"],
                revision=document["current_revision"],
                event_type="document.conflict_resolved",
                actor_principal=actor_principal,
                occurred_at=now,
                payload=payload,
            )
            connection.execute(
                """UPDATE document_conflicts SET status='resolved', resolved_event_seq=?,
                resolved_at=? WHERE conflict_id=?""",
                (event_seq, now, conflict_id),
            )
        return {
            "conflict_id": conflict_id,
            "status": "resolved",
            "event_seq": event_seq,
            "idempotent_replay": False,
        }

    @staticmethod
    def _require_vault_adapter(actor_principal: str) -> None:
        if actor_principal != VAULT_ADAPTER_PRINCIPAL:
            raise PublicationPolicyError("only the Vault Adapter principal may publish managed sections")

    @staticmethod
    def _enforce_fact_policy(fact: dict, target_scope: str) -> None:
        if fact["sensitivity"] == "restricted":
            raise PublicationPolicyError("restricted facts cannot be published to Obsidian")
        if fact["egress_policy"] == "local_only":
            raise PublicationPolicyError("local_only facts cannot be published to Obsidian")
        if target_scope == "shared" and fact["visibility"] == "owner_only":
            raise PublicationPolicyError("owner_only facts cannot be published to shared documents")

    @staticmethod
    def _validate_path(value: str) -> str:
        normalized = value.replace("\\", "/").strip().lstrip("/")
        path = PurePosixPath(normalized)
        if not normalized or any(part in {"", ".", ".."} for part in path.parts):
            raise PublicationPolicyError("vault_path must be a safe relative path")
        root = path.parts[0]
        if root in _FORBIDDEN_ROOTS or root not in _ALLOWED_ROOTS:
            raise PublicationPolicyError("vault_path is outside the publication allowlist")
        if path.suffix.lower() != ".md":
            raise PublicationPolicyError("only Markdown documents can be registered")
        return path.as_posix()

    @staticmethod
    def _insert_document_event(
        connection,
        *,
        event_id: str,
        document_id: str,
        revision: int,
        event_type: str,
        actor_principal: str,
        occurred_at: str,
        payload: dict,
    ) -> int:
        payload_json = canonical_json(payload)
        cursor = connection.execute(
            """INSERT INTO memory_events(
                event_id, aggregate_type, aggregate_id, aggregate_version,
                event_type, actor_principal, request_id, correlation_id,
                occurred_at, payload_json, payload_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                "document",
                document_id,
                revision,
                event_type,
                actor_principal,
                event_id,
                event_id,
                occurred_at,
                payload_json,
                sha256_text(payload_json),
            ),
        )
        return int(cursor.lastrowid)

    def _record_conflict(
        self,
        connection,
        *,
        publication: dict,
        document: dict,
        conflict_type: str,
        observed_revision: int,
        observed_hash: str | None,
        actor_principal: str,
    ) -> dict:
        now = utc_now()
        conflict_id = new_id()
        event_id = new_id()
        payload = {
            "document_id": document["document_id"],
            "publication_id": publication["publication_id"],
            "conflict_id": conflict_id,
            "conflict_type": conflict_type,
            "expected_revision": document["current_revision"],
            "actual_revision": observed_revision,
        }
        event_seq = self._insert_document_event(
            connection,
            event_id=event_id,
            document_id=document["document_id"],
            revision=document["current_revision"],
            event_type="document.conflict_detected",
            actor_principal=actor_principal,
            occurred_at=now,
            payload=payload,
        )
        connection.execute(
            """INSERT INTO document_conflicts(
                conflict_id, document_id, publication_id, conflict_type,
                expected_revision, actual_revision, expected_hash, actual_hash,
                status, detected_event_seq, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                conflict_id,
                document["document_id"],
                publication["publication_id"],
                conflict_type,
                document["current_revision"],
                observed_revision,
                publication["expected_previous_hash"],
                observed_hash,
                "open",
                event_seq,
                now,
            ),
        )
        connection.execute(
            "UPDATE document_publications SET status='conflict', completed_at=? WHERE publication_id=?",
            (now, publication["publication_id"]),
        )
        return {
            "publication_id": publication["publication_id"],
            "document_id": document["document_id"],
            "status": "conflict",
            "conflict_id": conflict_id,
            "conflict_type": conflict_type,
            "event_seq": event_seq,
        }


def validate_managed_sections(sections: dict[str, str]) -> dict[str, str]:
    if not isinstance(sections, dict) or not sections:
        raise PublicationPolicyError("at least one managed section is required")
    validated: dict[str, str] = {}
    for name, content in sections.items():
        if not isinstance(name, str) or not _SECTION_NAME.fullmatch(name):
            raise PublicationPolicyError(f"invalid managed section name: {name}")
        if not isinstance(content, str):
            raise PublicationPolicyError(f"managed section {name} must be text")
        if _START.split("{")[0] in content or _END.split("{")[0] in content:
            raise PublicationPolicyError("nested managed-section markers are forbidden")
        validated[name] = content.rstrip()
    return dict(sorted(validated.items()))


def publication_hash(sections: dict[str, str], fact_ids: tuple[str, ...]) -> str:
    return sha256_text(canonical_json({"sections": sections, "fact_ids": fact_ids}))


def replace_managed_sections(text: str, sections: dict[str, str]) -> str:
    result = text
    for name, content in validate_managed_sections(sections).items():
        start = _START.format(name=name)
        end = _END.format(name=name)
        if result.count(start) != result.count(end) or result.count(start) > 1:
            raise PublicationPolicyError(f"managed section markers are malformed: {name}")
        block = f"{start}\n{content}\n{end}"
        if start in result:
            pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
            result = pattern.sub(lambda _: block, result, count=1)
        else:
            separator = "\n\n" if result and not result.endswith("\n\n") else ""
            result = f"{result}{separator}{block}\n"
    return result


def human_content_hash(text: str) -> str:
    """Hash human-owned text while excluding complete machine-owned blocks."""
    pattern = re.compile(
        r"<!-- mimir:managed:start name=[a-z][a-z0-9_-]{0,63} -->.*?"
        r"<!-- mimir:managed:end name=[a-z][a-z0-9_-]{0,63} -->",
        re.DOTALL,
    )
    normalized = pattern.sub("", text).rstrip()
    return sha256_text(normalized)
