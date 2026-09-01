"""Governed three-layer knowledge services for Mímir v9.

Memory remains the canonical fact store. Learning and Wiki records are separate,
reviewable knowledge items. Unified search filters authorization inside each layer
before rank fusion. Feedback creates immutable signals and suggestions only; it
never mutates canonical facts or human Wiki content directly.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from .classifier import classify
from .learning import redact_text
from .query import QueryKernel, QueryRequest
from .schema import EGRESS_POLICIES, SENSITIVITIES, VISIBILITIES, ValidationError, get_registered_domains
from .store import CanonicalStore, ConflictError, canonical_json, new_id, sha256_text, utc_now


LAYERS = frozenset({"memory", "learning", "wiki"})
KNOWLEDGE_LAYERS = frozenset({"learning", "wiki"})
ITEM_STATUSES = frozenset({"active", "review", "archived", "quarantined"})
SIGNAL_TYPES = frozenset({
    "useful", "incorrect", "stale", "duplicate", "harmful", "withdraw",
    "explicit_reject", "seen_pending",
})
_TOKEN_PATTERN = re.compile(r"[\w\-\u4e00-\u9fff]+", re.UNICODE)


class SourceRoutingError(ValidationError):
    """Raised when a source is not allowed to enter the requested layer."""


@dataclass(frozen=True)
class SourceRoute:
    connector_type: str
    source_category: str
    default_layer: str
    allowed_layers: tuple[str, ...]
    policy_version: str = "v9-source-routing-r1"


class SourceRouter:
    """Fail-closed source-to-layer policy."""

    @staticmethod
    def route(connector_type: str, requested_layer: str | None = None) -> SourceRoute:
        connector = connector_type.strip() if isinstance(connector_type, str) else ""
        category = classify(connector)
        if category == "conversation":
            route = SourceRoute(connector, category, "memory", ("memory",))
        elif category == "external_info":
            route = SourceRoute(connector, category, "learning", ("learning",))
        elif category == "knowledge_doc":
            route = SourceRoute(connector, category, "wiki", ("learning", "wiki"))
        else:
            raise SourceRoutingError("unknown source is quarantined and cannot enter a knowledge layer")
        if requested_layer is not None and requested_layer not in route.allowed_layers:
            raise SourceRoutingError(
                f"source category {category} cannot enter layer {requested_layer}"
            )
        return route


@dataclass(frozen=True)
class CreateKnowledgeItem:
    connector_type: str
    layer: str | None
    title: str
    content: str
    owner_principal: str
    domain: str
    source_hash: str
    idempotency_key: str
    summary: str | None = None
    topics: tuple[str, ...] = ()
    status: str = "review"
    visibility: str = "owner_only"
    sensitivity: str = "internal"
    egress_policy: str = "local_only"
    source_uri: str | None = None
    policy_version: str = "v9-knowledge-r1"
    provenance: dict = field(default_factory=dict)
    stable_path: str | None = None
    file_sha256: str | None = None


class KnowledgeService:
    def __init__(self, store: CanonicalStore, router: SourceRouter | None = None):
        self.store = store
        self.router = router or SourceRouter()

    def create_item(
        self, command: CreateKnowledgeItem, actor_principal: str, *, is_admin: bool = False
    ) -> dict:
        actor = self._text("actor_principal", actor_principal, 128)
        route = self.router.route(command.connector_type, command.layer)
        layer = command.layer or route.default_layer
        if layer not in KNOWLEDGE_LAYERS:
            raise SourceRoutingError("conversation sources must use the canonical memory pipeline")
        title = self._text("title", command.title, 500)
        content_input = self._text("content", command.content, 500_000)
        summary_input = command.summary if command.summary is not None else content_input[:500]
        summary = self._text("summary", summary_input, 4_000)
        owner = self._text("owner_principal", command.owner_principal, 128)
        if actor != owner and not is_admin:
            raise PermissionError("cannot create knowledge for another principal")
        if command.status == "active" and not is_admin:
            raise PermissionError("knowledge activation requires governance review")
        if command.domain not in get_registered_domains():
            raise ValidationError(f"invalid domain: {command.domain}")
        if command.status not in ITEM_STATUSES:
            raise ValidationError(f"invalid status: {command.status}")
        if command.visibility not in VISIBILITIES:
            raise ValidationError(f"invalid visibility: {command.visibility}")
        if command.sensitivity not in SENSITIVITIES:
            raise ValidationError(f"invalid sensitivity: {command.sensitivity}")
        if command.egress_policy not in EGRESS_POLICIES:
            raise ValidationError(f"invalid egress_policy: {command.egress_policy}")
        if command.sensitivity == "restricted" and command.egress_policy != "local_only":
            raise ValidationError("restricted knowledge must use local_only egress")
        source_hash = self._sha256("source_hash", command.source_hash)
        key = self._text("idempotency_key", command.idempotency_key, 256)
        policy_version = self._text("policy_version", command.policy_version, 128)
        topics = self._topics(command.topics)
        redacted_content = redact_text(content_input)
        redacted_summary = redact_text(summary)
        redacted_title = redact_text(title)
        content_hash = sha256_text(redacted_content.text)
        now = utc_now()
        item_type = "learning_note" if layer == "learning" else "wiki_document"
        fingerprint = sha256_text(canonical_json({
            "connector_type": route.connector_type,
            "source_category": route.source_category,
            "layer": layer,
            "title": redacted_title.text,
            "content": redacted_content.text,
            "summary": redacted_summary.text,
            "owner_principal": owner,
            "domain": command.domain,
            "topics": topics,
            "status": command.status,
            "visibility": command.visibility,
            "sensitivity": command.sensitivity,
            "egress_policy": command.egress_policy,
            "source_uri": command.source_uri,
            "source_hash": source_hash,
            "policy_version": policy_version,
            "actor_principal": actor,
            "actor_is_admin": bool(is_admin),
            "provenance": command.provenance,
            "stable_path": command.stable_path,
            "file_sha256": command.file_sha256,
        }))
        with self.store.transaction() as connection:
            replay = connection.execute(
                "SELECT aggregate_id,payload_json,event_seq FROM memory_events WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if replay:
                payload = json.loads(replay["payload_json"])
                if payload.get("request_fingerprint") != fingerprint:
                    raise ConflictError("knowledge item idempotency key was reused with different content")
                return self._result(connection, replay["aggregate_id"], replay["event_seq"], True)
            duplicate = connection.execute(
                """SELECT item_id FROM knowledge_items
                   WHERE layer=? AND owner_principal=? AND source_hash=? AND content_hash=?""",
                (layer, owner, source_hash, content_hash),
            ).fetchone()
            item_id = duplicate["item_id"] if duplicate else new_id()
            event_type = "knowledge.item_deduplicated" if duplicate else "knowledge.item_created"
            event_id = new_id()
            payload = {
                "item_id": item_id,
                "layer": layer,
                "source_category": route.source_category,
                "content_hash": content_hash,
                "request_fingerprint": fingerprint,
                "deduplicated": bool(duplicate),
                "redaction_rules": sorted(set(
                    redacted_title.rules + redacted_summary.rules + redacted_content.rules
                )),
            }
            event_seq = self._event(
                connection, event_id, "knowledge_item", item_id, event_type,
                actor, key, now, payload,
            )
            if not duplicate:
                connection.execute(
                    """INSERT INTO knowledge_items(
                        item_id,layer,item_type,status,title,content,summary,owner_principal,
                        domain,topics_json,visibility,sensitivity,egress_policy,source_category,
                        source_uri,source_hash,content_hash,policy_version,provenance_json,
                        stable_path,file_sha256,created_by,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item_id, layer, item_type, command.status, redacted_title.text,
                        redacted_content.text, redacted_summary.text, owner, command.domain,
                        canonical_json(topics), command.visibility, command.sensitivity,
                        command.egress_policy, route.source_category,
                        command.source_uri.strip() if command.source_uri else None,
                        source_hash, content_hash, policy_version,
                        canonical_json(dict(command.provenance)),
                        command.stable_path.strip() if command.stable_path else None,
                        self._optional_sha256("file_sha256", command.file_sha256),
                        actor, now, now,
                    ),
                )
                self._owner_grants(connection, item_id, owner, actor, event_id, now)
            return self._result(connection, item_id, event_seq, False, bool(duplicate))

    def can_read(
        self, item_id: str, principal_id: str, *, is_admin: bool = False,
        roles: Iterable[str] = (),
    ) -> bool:
        if is_admin:
            return True
        with contextlib.closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT owner_principal,status FROM knowledge_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if not row or row["status"] not in {"active", "review"}:
                return False
            if row["owner_principal"] == principal_id:
                return True
            grants = connection.execute(
                """SELECT subject_type,subject_id,effect FROM resource_grants
                   WHERE resource_type='knowledge_item' AND resource_id=?
                   AND permission IN ('read','manage')
                   AND (expires_at IS NULL OR expires_at > ?)""",
                (item_id, utc_now()),
            ).fetchall()
        role_set = set(roles)
        effects = []
        for grant in grants:
            if grant["subject_type"] == "principal" and grant["subject_id"] == principal_id:
                effects.append(grant["effect"])
            elif grant["subject_type"] == "role" and grant["subject_id"] in role_set:
                effects.append(grant["effect"])
        return "deny" not in effects and "allow" in effects

    def get_item(
        self, item_id: str, principal_id: str, *, is_admin: bool = False,
        roles: Iterable[str] = (),
    ) -> dict:
        if not self.can_read(item_id, principal_id, is_admin=is_admin, roles=roles):
            raise PermissionError("knowledge item is not readable by this principal")
        with contextlib.closing(self.store.connect()) as connection:
            row = connection.execute("SELECT * FROM knowledge_items WHERE item_id=?", (item_id,)).fetchone()
            return dict(row)

    @staticmethod
    def _owner_grants(connection, item_id: str, owner: str, actor: str, event_id: str, now: str) -> None:
        for subject_type, subject_id, permission in (
            ("principal", owner, "read"),
            ("principal", owner, "write"),
            ("role", "admin", "manage"),
        ):
            connection.execute(
                """INSERT INTO resource_grants(
                    grant_id,resource_type,resource_id,subject_type,subject_id,permission,
                    effect,granted_by,created_at,source_event_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), "knowledge_item", item_id, subject_type, subject_id, permission,
                 "allow", actor, now, event_id),
            )

    @staticmethod
    def _event(connection, event_id, aggregate_type, aggregate_id, event_type, actor, key, now, payload) -> int:
        payload_json = canonical_json(payload)
        cursor = connection.execute(
            """INSERT INTO memory_events(
                event_id,aggregate_type,aggregate_id,aggregate_version,event_type,
                actor_principal,request_id,correlation_id,occurred_at,payload_json,payload_hash,
                idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, aggregate_type, aggregate_id, 1, event_type, actor, event_id, event_id,
             now, payload_json, sha256_text(payload_json), key),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _result(connection, item_id: str, event_seq: int, replay: bool, deduplicated: bool = False) -> dict:
        row = connection.execute(
            "SELECT item_id,layer,item_type,status,content_hash,source_category FROM knowledge_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ConflictError("knowledge item replay points to a missing item")
        return {
            **dict(row), "event_seq": int(event_seq), "idempotent_replay": replay,
            "content_deduplicated": deduplicated,
        }

    @staticmethod
    def _text(name: str, value: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{name} is required")
        cleaned = value.strip()
        if len(cleaned) > limit:
            raise ValidationError(f"{name} exceeds {limit} characters")
        return cleaned

    @staticmethod
    def _sha256(name: str, value: str) -> str:
        cleaned = KnowledgeService._text(name, value, 64).lower()
        if len(cleaned) != 64:
            raise ValidationError(f"{name} must be a SHA-256 hex digest")
        try:
            int(cleaned, 16)
        except ValueError as exc:
            raise ValidationError(f"{name} must be a SHA-256 hex digest") from exc
        return cleaned

    @staticmethod
    def _optional_sha256(name: str, value: str | None) -> str | None:
        return None if value is None else KnowledgeService._sha256(name, value)

    @staticmethod
    def _topics(values: Iterable[str]) -> list[str]:
        result = []
        seen = set()
        for value in values:
            cleaned = KnowledgeService._text("topic", value, 80)
            if cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
        if len(result) > 50:
            raise ValidationError("topics cannot exceed 50 items")
        return result


@dataclass(frozen=True)
class UnifiedSearchRequest:
    text: str
    principal_id: str
    limit: int = 10
    layers: tuple[str, ...] = ("memory", "learning", "wiki")
    roles: tuple[str, ...] = ()
    is_admin: bool = False
    domain: str | None = None
    use_vector: bool = True
    use_fts: bool = True
    use_graph: bool = True


class KnowledgeLayerSearch:
    def __init__(self, store: CanonicalStore, knowledge: KnowledgeService, layer: str):
        if layer not in KNOWLEDGE_LAYERS:
            raise ValueError("knowledge layer must be learning or wiki")
        self.store = store
        self.knowledge = knowledge
        self.layer = layer

    def search(self, request: UnifiedSearchRequest, candidate_limit: int) -> list[dict]:
        tokens = self._tokens(request.text)
        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM knowledge_items WHERE layer=? AND status IN ('active','review')
                   AND (? IS NULL OR domain=?) ORDER BY updated_at DESC,item_id LIMIT ?""",
                (self.layer, request.domain, request.domain, min(max(candidate_limit * 8, 50), 1000)),
            ).fetchall()
        visible = []
        for row in rows:
            item = dict(row)
            if not self.knowledge.can_read(
                item["item_id"], request.principal_id,
                is_admin=request.is_admin, roles=request.roles,
            ):
                continue
            score = self._local_score(item, tokens)
            if score <= 0:
                continue
            visible.append((score, item))
        visible.sort(key=lambda entry: (-entry[0], entry[1]["item_id"]))
        results = []
        for rank, (score, item) in enumerate(visible[:candidate_limit], 1):
            results.append({
                "layer": self.layer,
                "stable_id": item["item_id"],
                "type": item["item_type"],
                "title": item["title"],
                "content": item["content"],
                "summary": item["summary"],
                "owner_principal": item["owner_principal"],
                "domain": item["domain"],
                "source_uri": item["source_uri"],
                "visibility": item["visibility"],
                "sensitivity": item["sensitivity"],
                "egress_policy": item["egress_policy"],
                "content_hash": item["content_hash"],
                "updated_at": item["updated_at"],
                "layer_rank": rank,
                "layer_score": round(score, 6),
                "layer_score_explanation": {"algorithm": "token_field_match", "tokens": tokens},
            })
        return results

    @staticmethod
    def _tokens(text: str) -> list[str]:
        tokens = []
        seen = set()
        for token in _TOKEN_PATTERN.findall(text.lower()):
            if token not in seen:
                seen.add(token)
                tokens.append(token)
        if not tokens:
            raise ValidationError("search text has no searchable tokens")
        return tokens[:32]

    @staticmethod
    def _local_score(item: dict, tokens: list[str]) -> float:
        title = item["title"].lower()
        summary = item["summary"].lower()
        content = item["content"].lower()
        score = 0.0
        for token in tokens:
            score += 5.0 * title.count(token)
            score += 3.0 * summary.count(token)
            score += min(10, content.count(token))
        return score


class UnifiedSearch:
    def __init__(
        self, memory: QueryKernel, knowledge: KnowledgeService, *,
        enabled_layers: Iterable[str] = ("memory", "learning", "wiki"), rrf_k: int = 60,
    ):
        enabled = tuple(dict.fromkeys(enabled_layers))
        invalid = set(enabled) - LAYERS
        if invalid:
            raise ValueError(f"invalid enabled layers: {sorted(invalid)}")
        self.memory = memory
        self.knowledge = knowledge
        self.enabled_layers = enabled
        self.rrf_k = rrf_k
        self.adapters = {
            "learning": KnowledgeLayerSearch(knowledge.store, knowledge, "learning"),
            "wiki": KnowledgeLayerSearch(knowledge.store, knowledge, "wiki"),
        }

    def search(self, request: UnifiedSearchRequest) -> dict:
        query = request.text.strip()
        if not query:
            raise ValidationError("search text is required")
        if not 1 <= request.limit <= 100:
            raise ValidationError("limit must be between 1 and 100")
        requested = tuple(dict.fromkeys(request.layers))
        invalid = set(requested) - LAYERS
        if invalid:
            raise ValidationError(f"invalid layers: {sorted(invalid)}")
        fused = []
        layer_status = {}
        candidate_limit = max(20, request.limit * 5)
        for layer in requested:
            if layer not in self.enabled_layers:
                layer_status[layer] = {"status": "disabled", "count": 0}
                continue
            try:
                results = self._search_layer(layer, request, candidate_limit)
                layer_status[layer] = {"status": "ok", "count": len(results)}
                for result in results:
                    rank = result["layer_rank"]
                    contribution = 1.0 / (self.rrf_k + rank)
                    fused.append({
                        **result,
                        "score": round(contribution, 8),
                        "score_explanation": {
                            "fusion": "rrf_over_layer_local_rank",
                            "rrf_k": self.rrf_k,
                            "layer_rank": rank,
                            "rrf_contribution": round(contribution, 8),
                            "authorization": "filtered_before_ranking",
                            "layer_score": result["layer_score"],
                            "layer_score_explanation": result["layer_score_explanation"],
                        },
                    })
            except Exception as exc:
                layer_status[layer] = {
                    "status": "degraded", "count": 0, "error_code": type(exc).__name__,
                }
        fused.sort(key=lambda item: (-item["score"], item["layer"], item["stable_id"]))
        deduplicated = []
        seen_hashes = set()
        for item in fused:
            key = item.get("content_hash") or sha256_text(item["content"])
            if key in seen_hashes:
                continue
            seen_hashes.add(key)
            deduplicated.append(item)
            if len(deduplicated) >= request.limit:
                break
        return {
            "query": query,
            "principal_id": request.principal_id,
            "results": deduplicated,
            "layers": layer_status,
            "partial": any(value["status"] != "ok" for value in layer_status.values()),
            "fusion": {"algorithm": "rrf", "rrf_k": self.rrf_k, "score_comparable": True},
        }

    def _search_layer(self, layer: str, request: UnifiedSearchRequest, candidate_limit: int) -> list[dict]:
        if layer == "memory":
            result = self.memory.search(QueryRequest(
                text=request.text, principal_id=request.principal_id,
                limit=candidate_limit, candidate_limit=min(500, candidate_limit * 3),
                roles=request.roles, is_admin=request.is_admin, domain=request.domain,
                use_vector=request.use_vector, use_fts=request.use_fts, use_graph=request.use_graph,
            ))
            rows = []
            for rank, item in enumerate(result["results"], 1):
                rows.append({
                    "layer": "memory", "stable_id": item["fact_id"], "type": "fact",
                    "title": item["summary"], "content": item["content"],
                    "summary": item["summary"], "owner_principal": item["owner_principal"],
                    "domain": item["domain"], "source_uri": None,
                    "visibility": None, "sensitivity": None, "egress_policy": None,
                    "content_hash": sha256_text(item["content"]), "updated_at": None,
                    "layer_rank": rank, "layer_score": item["score"],
                    "layer_score_explanation": item["score_explanation"],
                })
            return rows
        return self.adapters[layer].search(request, candidate_limit)


class FeedbackLoop:
    def __init__(self, store: CanonicalStore, knowledge: KnowledgeService):
        self.store = store
        self.knowledge = knowledge

    def submit(
        self, *, target_layer: str, target_id: str, signal_type: str, signal_text: str,
        submitted_by: str, idempotency_key: str, is_admin: bool = False,
        roles: Iterable[str] = (),
    ) -> dict:
        if target_layer not in LAYERS:
            raise ValidationError(f"invalid target_layer: {target_layer}")
        if signal_type not in SIGNAL_TYPES:
            raise ValidationError(f"invalid signal_type: {signal_type}")
        target = KnowledgeService._text("target_id", target_id, 128)
        submitter = KnowledgeService._text("submitted_by", submitted_by, 128)
        text = redact_text(KnowledgeService._text("signal_text", signal_text, 10_000))
        key = KnowledgeService._text("idempotency_key", idempotency_key, 256)
        self._authorize_target(target_layer, target, submitter, is_admin, roles)
        fingerprint = sha256_text(canonical_json({
            "target_layer": target_layer, "target_id": target,
            "signal_type": signal_type, "signal_text": text.text,
            "submitted_by": submitter,
        }))
        now = utc_now()
        with self.store.transaction() as connection:
            replay = connection.execute(
                "SELECT * FROM knowledge_feedback_signals WHERE idempotency_key=?", (key,)
            ).fetchone()
            if replay:
                if replay["request_fingerprint"] != fingerprint:
                    raise ConflictError("feedback signal idempotency key was reused with different content")
                return self._feedback_result(connection, dict(replay), True)
            signal_id = new_id()
            event_id = new_id()
            payload = {
                "signal_id": signal_id, "target_layer": target_layer, "target_id": target,
                "signal_type": signal_type, "request_fingerprint": fingerprint,
                "redaction_rules": text.rules,
            }
            event_seq = KnowledgeService._event(
                connection, event_id, "knowledge_feedback", signal_id,
                "knowledge.feedback_submitted", submitter, key, now, payload,
            )
            connection.execute(
                """INSERT INTO knowledge_feedback_signals(
                    signal_id,target_layer,target_id,signal_type,signal_text,submitted_by,
                    idempotency_key,request_fingerprint,source_event_seq,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, target_layer, target, signal_type, text.text, submitter,
                 key, fingerprint, event_seq, now),
            )
            suggestion = self._create_suggestion(
                connection, signal_id, target_layer, target, signal_type, submitter, now
            )
            row = connection.execute(
                "SELECT * FROM knowledge_feedback_signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
            return self._feedback_result(connection, dict(row), False, suggestion)

    def _authorize_target(self, layer, target, principal, is_admin, roles) -> None:
        if layer == "memory":
            if not self.store.can_read(target, principal, is_admin=is_admin, roles=set(roles)):
                raise PermissionError("memory target is not readable by this principal")
        elif not self.knowledge.can_read(target, principal, is_admin=is_admin, roles=roles):
            raise PermissionError("knowledge target is not readable by this principal")

    @staticmethod
    def _create_suggestion(connection, signal_id, layer, target, signal_type, actor, now) -> dict | None:
        mapping = {
            ("learning", "useful"): "remember",
            ("memory", "incorrect"): "correct",
            ("memory", "stale"): "correct",
            ("memory", "harmful"): "forget",
            ("memory", "withdraw"): "forget",
            ("wiki", "incorrect"): "wiki_update",
            ("wiki", "stale"): "wiki_update",
            ("wiki", "harmful"): "wiki_update",
        }
        suggestion_type = mapping.get((layer, signal_type))
        if suggestion_type is None:
            return None
        suggestion_id = new_id()
        rationale = f"Observed explicit {signal_type} feedback; governance action requires review."
        connection.execute(
            """INSERT INTO governance_suggestions(
                suggestion_id,target_layer,target_id,suggestion_type,status,rationale,
                created_from_signal_id,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (suggestion_id, layer, target, suggestion_type, "open", rationale,
             signal_id, actor, now),
        )
        return {"suggestion_id": suggestion_id, "suggestion_type": suggestion_type, "status": "open"}

    @staticmethod
    def _feedback_result(connection, signal: dict, replay: bool, suggestion: dict | None = None) -> dict:
        if suggestion is None:
            row = connection.execute(
                """SELECT suggestion_id,suggestion_type,status FROM governance_suggestions
                   WHERE created_from_signal_id=?""", (signal["signal_id"],)
            ).fetchone()
            suggestion = dict(row) if row else None
        return {
            "signal_id": signal["signal_id"], "target_layer": signal["target_layer"],
            "target_id": signal["target_id"], "signal_type": signal["signal_type"],
            "source_event_seq": signal["source_event_seq"], "suggestion": suggestion,
            "idempotent_replay": replay,
            "canonical_mutated": False,
        }
