"""Versioned REST boundary and guarded v7 compatibility routes for Mímir v8."""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import AuthError, Principal, TokenStore
from .candidates import CandidatePolicyError, CandidateService, CreateCandidate, ReviewCandidate
from .learning import ConversationEnvelope, ConversationMessage, LearningService
from .knowledge import (
    CreateKnowledgeItem,
    FeedbackLoop,
    KnowledgeService,
    SourceRoutingError,
    UnifiedSearch,
    UnifiedSearchRequest,
)
from .extraction import EvidenceInput, ExtractionService
from .retention import RetentionSchedule, RetentionService
from .core_memory import (
    CoreMemoryPolicyError,
    CoreMemoryProjector,
    CoreMemoryService,
    PromoteCoreMemory,
    RetireCoreMemory,
)
from .query import QueryKernel, QueryRequest
from .schema import (
    CreateFact,
    GrantFactAccess,
    MIMIR_VERSION,
    SCHEMA_VERSION,
    TombstoneFact,
    UpdateFact,
    ValidationError,
)
from .store import CanonicalStore, ConflictError, NotFoundError, new_id, sha256_text, utc_now


@dataclass(frozen=True)
class ServiceContext:
    store: CanonicalStore
    token_store: TokenStore
    query: QueryKernel
    core_memory: CoreMemoryProjector | None = None
    candidates: CandidateService | None = None
    learning: LearningService | None = None
    extraction: ExtractionService | None = None
    retention: RetentionService | None = None
    core_memory_service: CoreMemoryService | None = None
    knowledge: KnowledgeService | None = None
    unified_search: UnifiedSearch | None = None
    feedback_loop: FeedbackLoop | None = None


class QueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10_000)
    limit: int = Field(default=10, ge=1, le=100)
    candidate_limit: int = Field(default=50, ge=1, le=500)
    owner_principal: str | None = None
    domain: str | None = None
    fact_type: str | None = None
    use_vector: bool = True
    use_fts: bool = True
    use_graph: bool = True
    include_provisional: bool = False


class CreateFactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    owner_principal: str
    domain: str
    fact_type: str
    summary: str | None = None
    visibility: str = "all"
    sensitivity: str = "internal"
    egress_policy: str = "local_only"
    project_id: str | None = None
    human_status: str = "unreviewed"
    confidence_score: float | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    source_kind: str | None = None
    source_uri: str | None = None
    source_hash: str | None = None
    idempotency_key: str | None = None


class UpdateFactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    content: str | None = None
    summary: str | None = None
    human_status: str | None = None
    confidence_score: float | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    change_reason: str = "canonical fact updated through REST API"
    idempotency_key: str | None = None


class TombstoneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str
    idempotency_key: str | None = None


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: str
    subject_id: str
    permission: str = "read"
    effect: str = "allow"
    expires_at: str | None = None
    idempotency_key: str | None = None


class CandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    idempotency_key: str


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    reason: str
    idempotency_key: str


class SetOpinionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str
    topic: str
    stance: str
    confidence: float
    owner_principal: str
    evidence_id: str | None = None


class ConsolidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GovernanceRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = False


class FastTrackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = "auto-approved via fast track"


class CommitCandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    idempotency_key: str


class ConversationMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    content: str
    principal_id: str | None = None
    created_at: str | None = None
    metadata: dict = Field(default_factory=dict)


class ConversationEnvelopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_type: str
    connector_id: str
    session_id: str | None = None
    owner_principal: str
    memory_mode: str = "observe"
    retention_class: str = "short"
    messages: tuple[ConversationMessageBody, ...]
    source_uri: str | None = None
    title: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    metadata: dict = Field(default_factory=dict)
    idempotency_key: str


class RememberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    owner_principal: str
    domain: str = "personal"
    fact_type: str = "user_pref"
    summary: str | None = None
    retention_class: str = "standard"
    idempotency_key: str


class ForgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str
    expected_version: int = Field(ge=1)
    reason: str
    idempotency_key: str


class CorrectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str
    expected_version: int = Field(ge=1)
    corrected_content: str
    summary: str | None = None
    reason: str
    idempotency_key: str


class LearningFeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback_type: str
    feedback_text: str
    candidate_id: str | None = None
    fact_id: str | None = None
    idempotency_key: str


class EvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    message_id: str
    quote_text: str
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


class ExtractCandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    source_id: str
    content: str
    owner_principal: str
    domain: str
    fact_type: str
    idempotency_key: str
    summary: str | None = None
    evidence: tuple[EvidenceBody, ...] = ()
    policy_version: str = "v8.1-default"


class RetentionScheduleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_type: str
    resource_id: str
    due_at: str
    reason: str
    legal_hold: bool = False
    idempotency_key: str


class RetentionActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class PromoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    block_name: str
    fact_id: str
    reason: str
    idempotency_key: str
    position: int = Field(default=0, ge=0)


class RetireBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    idempotency_key: str


class UnifiedSearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10_000)
    limit: int = Field(default=10, ge=1, le=100)
    layers: tuple[str, ...] = ("memory", "learning", "wiki")
    domain: str | None = None
    use_vector: bool = True
    use_fts: bool = True
    use_graph: bool = True


class KnowledgeItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_type: str
    layer: str | None = None
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
    provenance: dict = Field(default_factory=dict)
    stable_path: str | None = None
    file_sha256: str | None = None


class KnowledgeFeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_layer: str
    target_id: str
    signal_type: str
    signal_text: str
    idempotency_key: str


class AuthFailureLimiter:
    """P1-6 防爆破: per-client-IP auth-failure limiter.

    Tracks authentication failures (401) per client IP in a sliding window.
    Once failures reach ``max_failures`` the IP is locked for ``lock_seconds``
    and further requests are rejected with 429 before auth even runs.
    Successful auth clears the counter. In-memory only — a restart resets it,
    which is acceptable for a brute-force tripwire.
    """

    def __init__(self, max_failures: int = 10, window_seconds: float = 300.0,
                 lock_seconds: float = 300.0):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._lock = threading.Lock()
        # ip -> list of failure timestamps
        self._failures: dict[str, list[float]] = {}
        # ip -> lock expiry timestamp
        self._locked_until: dict[str, float] = {}

    def _client_ip(self, request: Request) -> str:
        if request.client and request.client.host:
            return request.client.host
        return "unknown"

    def is_locked(self, request: Request) -> bool:
        ip = self._client_ip(request)
        now = time.time()
        with self._lock:
            expiry = self._locked_until.get(ip)
            if expiry is None:
                return False
            if now >= expiry:
                self._locked_until.pop(ip, None)
                self._failures.pop(ip, None)
                return False
            return True

    def record_failure(self, request: Request) -> None:
        ip = self._client_ip(request)
        now = time.time()
        with self._lock:
            stamps = self._failures.setdefault(ip, [])
            stamps.append(now)
            cutoff = now - self.window_seconds
            self._failures[ip] = [t for t in stamps if t > cutoff]
            if len(self._failures[ip]) >= self.max_failures:
                self._locked_until[ip] = now + self.lock_seconds

    def record_success(self, request: Request) -> None:
        ip = self._client_ip(request)
        with self._lock:
            self._failures.pop(ip, None)


def create_app(context: ServiceContext, *, lifespan=None) -> FastAPI:
    app = FastAPI(
        title="Mímir REST API",
        version=MIMIR_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    # P1-6 防爆破: per-IP auth-failure limiter (10 failures / 5min -> 5min lock)
    auth_limiter = AuthFailureLimiter(max_failures=10, window_seconds=300.0, lock_seconds=300.0)

    def error_payload(request: Request, code: str, message: str, details: Any = None) -> dict:
        return {
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": getattr(request.state, "request_id", None),
            }
        }

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", "").strip() or str(uuid.uuid4())
        correlation_id = request.headers.get("X-Correlation-ID", "").strip() or request_id
        request.state.request_id = request_id[:128]
        request.state.correlation_id = correlation_id[:128]
        # P1-6 防爆破: reject locked IPs before any further processing.
        # Public health/readiness probes are exempt so monitoring stays alive.
        if auth_limiter.is_locked(request) and request.url.path not in (
            "/health", "/v8/health", "/ready", "/v8/ready",
        ):
            return JSONResponse(
                error_payload(request, "rate_limited", "too many authentication failures, try later"),
                status_code=429,
            )
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > 1_048_576:
                    return JSONResponse(
                        error_payload(request, "request_too_large", "request body exceeds 1 MiB"),
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    error_payload(request, "invalid_content_length", "invalid Content-Length header"),
                    status_code=400,
                )
        response = await call_next(request)
        # P1-6 防爆破: track auth outcomes per client IP. Only a request that
        # actually presented credentials can clear the failure counter — an
        # unauthenticated 2xx (e.g. /health) must not reset it.
        if response.status_code == 401:
            auth_limiter.record_failure(request)
        elif 200 <= response.status_code < 300 and request.headers.get("authorization"):
            auth_limiter.record_success(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _denial_actor(request: Request) -> str:
        try:
            return context.token_store.authenticate(request.headers.get("authorization")).principal_id
        except Exception:
            return "anonymous"

    def _audit_denial(request: Request, code: str, reason: str) -> None:
        try:
            context.store.write_audit(
                _denial_actor(request), f"security.{code}", str(request.url.path),
                {"method": request.method, "reason": reason}, outcome="denied",
            )
        except Exception:
            pass  # audit must never break the denial response

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        _audit_denial(request, exc.code, str(exc))
        return JSONResponse(error_payload(request, exc.code, str(exc)), status_code=exc.status_code)

    @app.exception_handler(SourceRoutingError)
    async def source_routing_error(request: Request, exc: SourceRoutingError):
        return JSONResponse(error_payload(request, "source_routing_error", str(exc)), status_code=422)

    @app.exception_handler(ValidationError)
    async def validation_error(request: Request, exc: ValidationError):
        return JSONResponse(error_payload(request, "validation_error", str(exc)), status_code=422)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            error_payload(request, "request_validation_error", "request validation failed", exc.errors()),
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            error_payload(request, "http_error", str(exc.detail)),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(ConflictError)
    async def conflict_error(request: Request, exc: ConflictError):
        return JSONResponse(error_payload(request, "conflict", str(exc)), status_code=409)

    @app.exception_handler(NotFoundError)
    async def not_found_error(request: Request, exc: NotFoundError):
        return JSONResponse(error_payload(request, "not_found", str(exc)), status_code=404)

    @app.exception_handler(CandidatePolicyError)
    @app.exception_handler(CoreMemoryPolicyError)
    async def policy_error(request: Request, exc: ValueError):
        _audit_denial(request, "policy_rejected", str(exc))
        return JSONResponse(error_payload(request, "policy_rejected", str(exc)), status_code=403)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return JSONResponse(error_payload(request, "validation_error", str(exc)), status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        return JSONResponse(
            error_payload(request, "internal_error", "internal service error", type(exc).__name__),
            status_code=500,
        )

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        return context.token_store.authenticate(authorization)

    def scoped(scope: str):
        def dependency(identity: Principal = Depends(principal)) -> Principal:
            identity.require(scope)
            return identity
        return dependency

    def require_fact_permission(identity: Principal, fact: dict, permission: str) -> None:
        if not identity.can_act_as(fact["owner_principal"]):
            raise AuthError(
                f"cannot {permission} another principal's fact", 403, "owner_boundary"
            )
        if not context.store.can_access(
            fact["fact_id"], identity.principal_id, permission,
            is_admin=identity.is_admin, roles=set(identity.roles),
        ):
            raise AuthError(
                f"fact {permission} permission is denied", 403, "acl_denied"
            )

    def health_payload() -> dict:
        return {
            "status": "ok",
            "service": "mimir-api",
            "version": MIMIR_VERSION,
            "schema_version": SCHEMA_VERSION,
        }

    def projector_status() -> list[dict]:
        with contextlib.closing(context.store.connect()) as connection:
            rows = connection.execute(
                """SELECT p.projector_name, p.checkpoint_event_seq, p.status,
                p.last_error_code,
                SUM(CASE WHEN o.status IN ('pending','retry') THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN o.status='dead_letter' THEN 1 ELSE 0 END) AS dead_letter
                FROM projector_state p LEFT JOIN outbox o
                ON o.projector_name=p.projector_name
                GROUP BY p.projector_name, p.checkpoint_event_seq, p.status, p.last_error_code
                ORDER BY p.projector_name"""
            ).fetchall()
        return [
            {
                **dict(row),
                "pending": int(row["pending"] or 0),
                "dead_letter": int(row["dead_letter"] or 0),
            }
            for row in rows
        ]

    def visible_stats(identity: Principal) -> dict:
        with contextlib.closing(context.store.connect()) as connection:
            rows = connection.execute(
                "SELECT fact_id, owner_principal, domain, fact_type, status FROM facts ORDER BY fact_id"
            ).fetchall()
        visible = [
            row for row in rows
            if row["status"] == "active" and context.store.can_read(
                row["fact_id"], identity.principal_id,
                is_admin=identity.is_admin, roles=set(identity.roles),
            )
        ]
        def grouped(key: str) -> dict[str, int]:
            result: dict[str, int] = {}
            for row in visible:
                result[row[key]] = result.get(row[key], 0) + 1
            return result
        return {
            "total_facts": len(visible),
            "by_agent": grouped("owner_principal"),
            "by_domain": grouped("domain"),
            "by_type": grouped("fact_type"),
        }

    def awareness_rows(identity: Principal, agent_id: str, hours: int | None, limit: int = 50) -> list[dict]:
        if not identity.can_act_as(agent_id):
            raise AuthError("cannot act as another agent", 403, "owner_boundary")
        with contextlib.closing(context.store.connect()) as connection:
            if hours is None:
                rows = connection.execute(
                    """SELECT * FROM facts WHERE status='active' AND owner_principal != ?
                    ORDER BY updated_at DESC, fact_id LIMIT ?""",
                    (agent_id, limit * 4),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM facts WHERE status='active' AND owner_principal != ?
                    AND updated_at >= datetime('now', ?)
                    ORDER BY updated_at DESC, fact_id LIMIT ?""",
                    (agent_id, f"-{hours} hours", limit * 4),
                ).fetchall()
        visible = []
        for row in rows:
            if not context.store.can_read(
                row["fact_id"], identity.principal_id,
                is_admin=identity.is_admin, roles=set(identity.roles),
            ):
                continue
            visible.append({
                "fact_id": row["fact_id"],
                "summary": row["summary"],
                "agent_id": row["owner_principal"],
                "domain": row["domain"],
                "fact_type": row["fact_type"],
                "updated_at": row["updated_at"],
            })
            if len(visible) >= limit:
                break
        return visible

    def execute_query(body: QueryBody, identity: Principal) -> dict:
        if body.owner_principal and not identity.can_act_as(body.owner_principal):
            # Filtering by another owner is safe for shared facts, so do not treat it
            # as impersonation. Authorization still happens during canonical hydration.
            pass
        result = context.query.search(QueryRequest(
            text=body.text,
            principal_id=identity.principal_id,
            limit=body.limit,
            candidate_limit=body.candidate_limit,
            roles=tuple(identity.roles),
            is_admin=identity.is_admin,
            owner_principal=body.owner_principal,
            domain=body.domain,
            fact_type=body.fact_type,
            use_vector=body.use_vector,
            use_fts=body.use_fts,
            use_graph=body.use_graph,
            include_provisional=body.include_provisional,
        ))
        try:
            context.store.write_audit(
                identity.principal_id, "query", body.text[:120],
                payload={"hits": int(result.get("total", 0) or 0),
                         "limit": body.limit},
            )
        except Exception:
            pass
        return result

    @app.get("/v8/health")
    @app.get("/health")
    def health():
        return health_payload()

    @app.get("/v8/ready")
    @app.get("/ready")
    def ready(request: Request):
        try:
            auth_principals = context.token_store.validate()
            counts = context.store.counts()
            statuses = projector_status()
            pending = sum(item["pending"] for item in statuses)
            dead_letters = sum(item["dead_letter"] for item in statuses)
            status = (
                "ready"
                if auth_principals > 0 and pending == 0 and dead_letters == 0
                else "not_ready"
            )
            payload = {
                "status": status,
                "facts": counts["facts"],
                "event_head": context.store.event_head(),
                "principals": auth_principals,
                "pending": pending,
                "dead_letters": dead_letters,
                "projectors": statuses,
            }
            if status != "ready":
                return JSONResponse(payload, status_code=503)
            return payload
        except AuthError:
            raise
        except Exception as exc:
            return JSONResponse(
                error_payload(request, "readiness_failed", "readiness check failed", type(exc).__name__),
                status_code=503,
            )

    @app.get("/v8/projectors")
    def get_projectors(identity: Principal = Depends(scoped("admin"))):
        return {"projectors": projector_status(), "event_head": context.store.event_head()}

    @app.post("/v8/query")
    def query_v8(body: QueryBody, identity: Principal = Depends(scoped("read"))):
        return execute_query(body, identity)

    @app.get("/v8/memories/recent")
    def memories_recent(
        limit: int = Query(default=10, ge=1, le=100),
        owner_principal: str | None = Query(default=None),
        identity: Principal = Depends(scoped("read")),
    ):
        rows = _recent_memories(context, identity, limit, owner_principal)
        return {"limit": limit, "total": len(rows), "results": rows, "channels": {"facts": True}}

    def _recent_memories(context, identity, limit, owner_principal) -> list[dict]:
        with context.store.connect() as connection:
            params: list[str] = []
            where = "status='active'"
            if owner_principal:
                where += " AND owner_principal=?"
                params.append(owner_principal)
            rows = connection.execute(
                f"SELECT fact_id, content, summary, domain, fact_type, recorded_at, "
                f"updated_at, confidence_score FROM facts WHERE {where} "
                f"ORDER BY updated_at DESC, recorded_at DESC LIMIT ?",
                (*params, max(limit, 200)),
            ).fetchall()
        roles = set(identity.roles)
        result = []
        for fact in rows:
            if not context.store.can_read(
                fact["fact_id"], identity.principal_id,
                is_admin=identity.is_admin, roles=roles,
            ):
                continue
            result.append({
                "fact_id": fact["fact_id"],
                "content": fact["content"],
                "summary": fact["summary"],
                "domain": fact["domain"],
                "fact_type": fact["fact_type"],
                "recorded_at": fact["recorded_at"],
                "updated_at": fact["updated_at"],
                "confidence_score": fact["confidence_score"],
            })
            if len(result) >= limit:
                break
        return result

    @app.get("/v8/facts/{fact_id}")
    def get_fact(fact_id: str, identity: Principal = Depends(scoped("read"))):
        fact = context.store.get_fact(fact_id)
        if not context.store.can_read(
            fact_id, identity.principal_id,
            is_admin=identity.is_admin, roles=set(identity.roles),
        ):
            raise AuthError("fact is not readable by this principal", 403, "acl_denied")
        return fact

    @app.post("/v8/facts", status_code=201)
    def create_fact(body: CreateFactBody, request: Request,
                    identity: Principal = Depends(scoped("write"))):
        if not identity.can_act_as(body.owner_principal):
            raise AuthError("cannot create a fact for another principal", 403, "owner_boundary")
        return context.store.create_fact(
            CreateFact(**body.model_dump()),
            actor_principal=identity.principal_id,
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
        )

    @app.patch("/v8/facts/{fact_id}")
    def update_fact(fact_id: str, body: UpdateFactBody, request: Request,
                    identity: Principal = Depends(scoped("write"))):
        fact = context.store.get_fact(fact_id)
        require_fact_permission(identity, fact, "write")
        return context.store.update_fact(
            UpdateFact(fact_id=fact_id, **body.model_dump()),
            actor_principal=identity.principal_id,
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
        )

    @app.post("/v8/facts/{fact_id}/tombstone")
    def tombstone_fact(fact_id: str, body: TombstoneBody, request: Request,
                       identity: Principal = Depends(scoped("delete"))):
        fact = context.store.get_fact(fact_id)
        require_fact_permission(identity, fact, "delete")
        return context.store.tombstone_fact(
            TombstoneFact(fact_id=fact_id, **body.model_dump()),
            actor_principal=identity.principal_id,
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
        )

    @app.post("/v8/facts/{fact_id}/grants")
    def grant_access(fact_id: str, body: GrantBody, request: Request,
                     identity: Principal = Depends(scoped("manage"))):
        if not identity.is_admin:
            raise AuthError("ACL management requires admin", 403, "admin_required")
        return context.store.grant_fact_access(
            GrantFactAccess(fact_id=fact_id, **body.model_dump()),
            actor_principal=identity.principal_id,
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
        )

    @app.post("/v8/ingestion/conversations", status_code=201)
    def ingest_conversation(body: ConversationEnvelopeBody, identity: Principal = Depends(scoped("ingest"))):
        if context.learning is None:
            raise HTTPException(503, "learning service is not configured")
        if not identity.can_act_as(body.owner_principal):
            raise AuthError("cannot ingest for another principal", 403, "owner_boundary")
        envelope = ConversationEnvelope(
            connector_type=body.connector_type,
            connector_id=body.connector_id,
            session_id=body.session_id,
            owner_principal=body.owner_principal,
            memory_mode=body.memory_mode,
            retention_class=body.retention_class,
            messages=tuple(ConversationMessage(**item.model_dump()) for item in body.messages),
            source_uri=body.source_uri,
            title=body.title,
            started_at=body.started_at,
            ended_at=body.ended_at,
            metadata=body.metadata,
            idempotency_key=body.idempotency_key,
        )
        return context.learning.ingest_conversation(envelope, identity.principal_id)

    @app.post("/v8/learning/remember", status_code=201)
    def remember(body: RememberBody, identity: Principal = Depends(scoped("write"))):
        if context.learning is None:
            raise HTTPException(503, "learning service is not configured")
        if not identity.can_act_as(body.owner_principal):
            raise AuthError("cannot remember for another principal", 403, "owner_boundary")
        return context.learning.remember(**body.model_dump(), actor_principal=identity.principal_id)

    @app.post("/v8/learning/forget")
    def forget(body: ForgetBody, identity: Principal = Depends(scoped("delete"))):
        if context.learning is None:
            raise HTTPException(503, "learning service is not configured")
        fact = context.store.get_fact(body.fact_id)
        require_fact_permission(identity, fact, "delete")
        return context.learning.forget(**body.model_dump(), actor_principal=identity.principal_id)

    @app.post("/v8/learning/correct", status_code=201)
    def correct(body: CorrectBody, identity: Principal = Depends(scoped("write"))):
        if context.learning is None:
            raise HTTPException(503, "learning service is not configured")
        fact = context.store.get_fact(body.fact_id)
        require_fact_permission(identity, fact, "write")
        return context.learning.correct(**body.model_dump(), actor_principal=identity.principal_id)

    @app.get("/v8/learning/candidates")
    def learning_candidates(status: str | None = None, limit: int = Query(default=50, ge=1, le=200), identity: Principal = Depends(scoped("review"))):
        with contextlib.closing(context.store.connect()) as connection:
            query = "SELECT candidate_id,status,summary,proposed_owner_principal,proposed_domain,proposed_fact_type,confidence_score,created_at,updated_at FROM candidate_facts"
            params = []
            if status:
                query += " WHERE status=?"
                params.append(status)
            query += " ORDER BY updated_at DESC, candidate_id LIMIT ?"
            params.append(limit)
            rows = [dict(row) for row in connection.execute(query, params).fetchall()]
        visible = [row for row in rows if identity.is_admin or identity.can_act_as(row["proposed_owner_principal"])]
        return {"candidates": visible, "count": len(visible)}

    @app.post("/v8/learning/feedback", status_code=201)
    def learning_feedback(body: LearningFeedbackBody, identity: Principal = Depends(scoped("write"))):
        if context.learning is None:
            raise HTTPException(503, "learning service is not configured")
        if body.fact_id:
            fact = context.store.get_fact(body.fact_id)
            require_fact_permission(identity, fact, "read")
        if body.candidate_id and not identity.is_admin:
            with contextlib.closing(context.store.connect()) as connection:
                candidate = connection.execute(
                    "SELECT proposed_owner_principal FROM candidate_facts WHERE candidate_id=?",
                    (body.candidate_id,),
                ).fetchone()
            if not candidate:
                raise NotFoundError(body.candidate_id)
            if not identity.can_act_as(candidate["proposed_owner_principal"]):
                raise AuthError("cannot submit feedback for another principal's candidate", 403, "owner_boundary")
        return context.learning.submit_feedback(**body.model_dump(), submitted_by=identity.principal_id)

    @app.post("/v8/learning/extractions", status_code=201)
    def extract_candidate(body: ExtractCandidateBody, identity: Principal = Depends(scoped("ingest"))):
        if context.extraction is None:
            raise HTTPException(503, "extraction service is not configured")
        if not identity.can_act_as(body.owner_principal):
            raise AuthError("cannot extract for another principal", 403, "owner_boundary")
        return context.extraction.extract_candidate(
            run_id=body.run_id, source_id=body.source_id, actor_principal=identity.principal_id,
            content=body.content, owner_principal=body.owner_principal, domain=body.domain,
            fact_type=body.fact_type, idempotency_key=body.idempotency_key, summary=body.summary,
            evidence=tuple(EvidenceInput(**item.model_dump()) for item in body.evidence),
            policy_version=body.policy_version,
        )

    @app.post("/v8/learning/retention", status_code=201)
    def schedule_retention(body: RetentionScheduleBody, identity: Principal = Depends(scoped("manage"))):
        if context.retention is None:
            raise HTTPException(503, "retention service is not configured")
        if not identity.is_admin:
            raise AuthError("retention scheduling requires admin", 403, "admin_required")
        return context.retention.schedule(RetentionSchedule(**body.model_dump()), identity.principal_id)

    @app.post("/v8/learning/retention/execute")
    def execute_retention(limit: int = Query(default=50, ge=1, le=500), identity: Principal = Depends(scoped("manage"))):
        if context.retention is None:
            raise HTTPException(503, "retention service is not configured")
        if not identity.is_admin:
            raise AuthError("retention execution requires admin", 403, "admin_required")
        return context.retention.execute_due(identity.principal_id, limit=limit)

    @app.post("/v8/learning/retention/{retention_job_id}/release")
    def release_retention_hold(retention_job_id: str, body: RetentionActionBody,
                               identity: Principal = Depends(scoped("manage"))):
        if context.retention is None:
            raise HTTPException(503, "retention service is not configured")
        if not identity.is_admin:
            raise AuthError("retention hold release requires admin", 403, "admin_required")
        return context.retention.release_hold(retention_job_id, identity.principal_id, body.reason)

    @app.post("/v8/learning/retention/{retention_job_id}/cancel")
    def cancel_retention(retention_job_id: str, body: RetentionActionBody,
                         identity: Principal = Depends(scoped("manage"))):
        if context.retention is None:
            raise HTTPException(503, "retention service is not configured")
        if not identity.is_admin:
            raise AuthError("retention cancellation requires admin", 403, "admin_required")
        return context.retention.cancel(retention_job_id, identity.principal_id, body.reason)

    @app.get("/v8/learning/status")
    def learning_status(identity: Principal = Depends(scoped("read"))):
        with contextlib.closing(context.store.connect()) as connection:
            return {
                "schema_version": SCHEMA_VERSION,
                "candidates": dict(connection.execute("SELECT status, COUNT(*) FROM candidate_facts GROUP BY status").fetchall()),
                "ingestion_runs": dict(connection.execute("SELECT status, COUNT(*) FROM ingestion_runs GROUP BY status").fetchall()),
                "extraction_runs": dict(connection.execute("SELECT status, COUNT(*) FROM extraction_runs GROUP BY status").fetchall()),
                "feedback": connection.execute("SELECT COUNT(*) FROM learning_feedback").fetchone()[0],
                "retention_jobs": dict(connection.execute("SELECT status, COUNT(*) FROM retention_jobs GROUP BY status").fetchall()),
            }

    @app.post("/v8/candidates", status_code=201)
    def create_candidate(body: CandidateBody, identity: Principal = Depends(scoped("write"))):
        if context.candidates is None:
            raise HTTPException(503, "candidate service is not configured")
        if not identity.can_act_as(body.proposed_owner_principal):
            raise AuthError("cannot propose a candidate for another principal", 403, "owner_boundary")
        return context.candidates.create_candidate(
            CreateCandidate(**body.model_dump()), identity.principal_id
        )

    def require_candidate_owner(candidate_id: str, identity: Principal) -> None:
        with contextlib.closing(context.store.connect()) as connection:
            candidate = connection.execute(
                "SELECT proposed_owner_principal FROM candidate_facts WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        if not candidate:
            raise NotFoundError(candidate_id)
        if not identity.can_act_as(candidate["proposed_owner_principal"]):
            raise AuthError("cannot govern another principal's candidate", 403, "owner_boundary")

    @app.post("/v8/learning/candidates/{candidate_id}/review")
    @app.post("/v8/candidates/{candidate_id}/review")
    def review_candidate(candidate_id: str, body: ReviewBody,
                         identity: Principal = Depends(scoped("review"))):
        if context.candidates is None:
            raise HTTPException(503, "candidate service is not configured")
        require_candidate_owner(candidate_id, identity)
        return context.candidates.review_candidate(
            ReviewCandidate(candidate_id=candidate_id, **body.model_dump()), identity.principal_id
        )

    @app.post("/v8/learning/candidates/{candidate_id}/commit")
    @app.post("/v8/candidates/{candidate_id}/commit")
    def commit_candidate(candidate_id: str, body: CommitCandidateBody,
                         identity: Principal = Depends(scoped("review"))):
        if context.candidates is None:
            raise HTTPException(503, "candidate service is not configured")
        require_candidate_owner(candidate_id, identity)
        return context.candidates.commit_approved(
            candidate_id, identity.principal_id, body.idempotency_key
        )

    @app.get("/v8/core-memory/{agent_id}/inject")
    def core_memory_inject(agent_id: str, max_chars: int = Query(default=2000, ge=64, le=20_000),
                           identity: Principal = Depends(scoped("read"))):
        if not identity.can_act_as(agent_id):
            raise AuthError("CoreMemory is owner-only", 403, "owner_boundary")
        if context.core_memory is None:
            raise HTTPException(503, "CoreMemory projection is not configured")
        return {
            "agent_id": agent_id,
            "injection_text": context.core_memory.injection_text(agent_id, max_chars),
        }

    @app.post("/v8/core-memory/promotions", status_code=201)
    def promote_core_memory(body: PromoteBody,
                            identity: Principal = Depends(scoped("core_memory"))):
        if context.core_memory_service is None:
            raise HTTPException(503, "CoreMemory service is not configured")
        return context.core_memory_service.promote(
            PromoteCoreMemory(**body.model_dump()), identity.principal_id,
            is_admin=identity.is_admin,
        )

    @app.post("/v8/core-memory/items/{item_id}/retire")
    def retire_core_memory(item_id: str, body: RetireBody,
                           identity: Principal = Depends(scoped("core_memory"))):
        if context.core_memory_service is None:
            raise HTTPException(503, "CoreMemory service is not configured")
        return context.core_memory_service.retire(
            RetireCoreMemory(item_id=item_id, **body.model_dump()), identity.principal_id,
            is_admin=identity.is_admin,
        )

    @app.post("/v9/search-preview")
    def search_v9_preview(
        body: UnifiedSearchBody,
        identity: Principal = Depends(scoped("read")),
    ):
        if context.unified_search is None:
            raise HTTPException(503, "unified search is not configured")
        return context.unified_search.search(UnifiedSearchRequest(
            text=body.text,
            principal_id=identity.principal_id,
            limit=body.limit,
            layers=body.layers,
            roles=tuple(identity.roles),
            is_admin=identity.is_admin,
            domain=body.domain,
            use_vector=body.use_vector,
            use_fts=body.use_fts,
            use_graph=body.use_graph,
        ))

    @app.post("/v9/knowledge/items", status_code=201)
    def create_knowledge_item(
        body: KnowledgeItemBody,
        identity: Principal = Depends(scoped("ingest")),
    ):
        if context.knowledge is None:
            raise HTTPException(503, "knowledge service is not configured")
        if not identity.can_act_as(body.owner_principal):
            raise AuthError("cannot ingest knowledge for another principal", 403, "owner_boundary")
        if body.status not in {"review", "quarantined"}:
            raise AuthError(
                "API knowledge ingestion cannot bypass review",
                403,
                "review_required",
            )
        return context.knowledge.create_item(
            CreateKnowledgeItem(**body.model_dump()),
            actor_principal=identity.principal_id,
            is_admin=identity.is_admin,
        )

    @app.get("/v9/knowledge/items/{item_id}")
    def get_knowledge_item(
        item_id: str,
        identity: Principal = Depends(scoped("read")),
    ):
        if context.knowledge is None:
            raise HTTPException(503, "knowledge service is not configured")
        try:
            return context.knowledge.get_item(
                item_id,
                identity.principal_id,
                is_admin=identity.is_admin,
                roles=identity.roles,
            )
        except PermissionError as exc:
            raise AuthError("knowledge item is not readable", 403, "acl_denied") from exc

    @app.post("/v9/knowledge/feedback", status_code=201)
    def submit_knowledge_feedback(
        body: KnowledgeFeedbackBody,
        identity: Principal = Depends(scoped("write")),
    ):
        if context.feedback_loop is None:
            raise HTTPException(503, "knowledge feedback is not configured")
        try:
            return context.feedback_loop.submit(
                **body.model_dump(),
                submitted_by=identity.principal_id,
                is_admin=identity.is_admin,
                roles=identity.roles,
            )
        except PermissionError as exc:
            raise AuthError("knowledge feedback target is not readable", 403, "acl_denied") from exc

    @app.get("/v9/knowledge/status")
    def knowledge_status(identity: Principal = Depends(scoped("admin"))):
        enabled = list(context.unified_search.enabled_layers) if context.unified_search else []
        with contextlib.closing(context.store.connect()) as connection:
            counts = dict(connection.execute(
                "SELECT layer,COUNT(*) FROM knowledge_items GROUP BY layer ORDER BY layer"
            ).fetchall())
            suggestions = dict(connection.execute(
                "SELECT status,COUNT(*) FROM governance_suggestions GROUP BY status ORDER BY status"
            ).fetchall())
        return {
            "schema_version": SCHEMA_VERSION,
            "preview": True,
            "enabled_layers": enabled,
            "knowledge_items": counts,
            "governance_suggestions": suggestions,
            "canonical_pipeline_unchanged": True,
        }

    @app.get("/v10/opinions")
    def list_opinions(owner: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                      identity: Principal = Depends(scoped("read"))):
        with contextlib.closing(context.store.connect()) as conn:
            query = "SELECT * FROM opinions"
            params = []
            if owner:
                query += " WHERE owner_principal=?"
                params.append(owner)
            query += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
            params.append(limit)
            rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        visible = [r for r in rows if identity.is_admin or identity.can_act_as(r["owner_principal"])]
        return {"opinions": visible, "count": len(visible)}

    @app.get("/v10/opinions/{fact_id}")
    def get_opinion(fact_id: str, identity: Principal = Depends(scoped("read"))):
        with contextlib.closing(context.store.connect()) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM opinions WHERE fact_id=? ORDER BY confidence DESC", (fact_id,)
            ).fetchall()]
        visible = [r for r in rows if identity.is_admin or identity.can_act_as(r["owner_principal"])]
        return {"opinions": visible, "count": len(visible)}

    @app.post("/v10/opinions", status_code=201)
    def set_opinion(body: SetOpinionBody, identity: Principal = Depends(scoped("write"))):
        if not identity.can_act_as(body.owner_principal):
            raise AuthError("cannot set opinion for another principal", 403, "owner_boundary")
        from .opinion import OpinionService
        result = OpinionService(context.store).set_opinion(
            fact_id=body.fact_id, topic=body.topic, stance=body.stance,
            confidence=body.confidence, owner_principal=body.owner_principal,
            evidence_id=body.evidence_id, actor_principal=identity.principal_id,
        )
        return result

    @app.get("/v10/observations")
    def list_observations(owner: str | None = None, limit: int = Query(default=20, ge=1, le=100),
                          identity: Principal = Depends(scoped("read"))):
        with contextlib.closing(context.store.connect()) as conn:
            query = "SELECT * FROM observations WHERE stale=0"
            params = []
            if owner:
                query += " AND owner_principal=?"
                params.append(owner)
            query += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
            params.append(limit)
            rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        visible = [r for r in rows if identity.is_admin or identity.can_act_as(r["owner_principal"])]
        return {"observations": visible, "count": len(visible)}

    @app.post("/v10/observations/consolidate", status_code=201)
    def consolidate_observations(body: ConsolidateBody,
                                  identity: Principal = Depends(scoped("manage"))):
        from .opinion import OpinionService
        result = OpinionService(context.store).consolidate_observations()
        return result

    @app.post("/v10/governance/run")
    def run_governance(body: GovernanceRunBody,
                       identity: Principal = Depends(scoped("manage"))):
        from .governance import run_governance_once
        from .candidates import CandidateService
        svc = CandidateService(context.store)
        result = run_governance_once(context.store, svc, dry_run=body.dry_run, actor=identity.principal_id)
        return result

    @app.post("/v10/candidates/{candidate_id}/fast_track")
    def fast_track_candidate(candidate_id: str, body: FastTrackBody,
                             identity: Principal = Depends(scoped("manage"))):
        from .candidates import CandidateService, ReviewCandidate
        svc = CandidateService(context.store)
        result = svc.review_candidate(
            ReviewCandidate(candidate_id=candidate_id, action="approve",
                            reason=f"[fast_track] {body.reason}",
                            idempotency_key=f"fasttrack-{candidate_id}-{utc_now()}"),
            identity.principal_id,
        )
        from .opinion import OpinionService
        OpinionService(context.store).set_opinion(
            fact_id=candidate_id, topic=f"fast_track:{body.reason}",
            stance="support", confidence=0.8, owner_principal=identity.principal_id,
        )
        return result

    @app.post("/v10/reflect/{topic}")
    def reflect(topic: str, body: dict | None = None, identity: Principal = Depends(scoped("write"))):
        """龙卷— synthesize an insight by fusing related facts + opinions on a topic."""
        from .opinion import OpinionService
        svc = OpinionService(context.store)
        limit = (body or {}).get("limit", 5) if body else 5
        qres = execute_query(QueryBody(
            text=topic, limit=limit, candidate_limit=50,
            owner_principal=identity.principal_id,
        ), identity)
        facts = [r for r in qres["results"] if r.get("status", "active") in ("active", "provisional")]
        fact_ids = [f["fact_id"] for f in facts]
        opinions = svc.get_opinions_for_facts(fact_ids) if fact_ids else []
        op_by_fact: dict[str, list[dict]] = {}
        for o in opinions:
            op_by_fact.setdefault(o["fact_id"], []).append(dict(o))
        insights = []
        for f in facts[:limit]:
            entry = {
                "fact_id": f["fact_id"], "content": (f.get("summary") or f.get("content") or "")[:160],
                "score": round(float(f.get("score", 0) or 0), 4),
                "opinions": op_by_fact.get(f["fact_id"], []),
            }
            insights.append(entry)
        summary = (
            f"Synthesized {len(insights)} supporting fact(s) for topic '{topic}' "
            f"with {len(opinions)} related opinion(s)."
        )
        with contextlib.suppress(Exception):
            context.store.write_audit(
                actor=identity.principal_id, action="reflect", target=topic,
                payload={"hits": len(insights), "opinions": len(opinions)},
            )
        return {"status": "ok", "topic": topic, "insight": summary,
                "supporting_facts": insights, "related_opinions": len(opinions)}

    @app.post("/v10/federation/{peer_hierarchy:path}")
    def federation(peer_hierarchy: str, body: dict | None = None,
                   identity: Principal = Depends(scoped("write"))):
        """联邦查询 — cross-principal shared search scoped by hierarchy, ACL enforced."""
        summary = None
        rows = []
        peers = [p for p in peer_hierarchy.split("/") if p]
        owners = (body or {}).get("owners") if body else None
        total = 0
        try:
            with context.store.transaction() as conn:
                if owners:
                    placeholders = ",".join("?" for _ in owners)
                    rows = conn.execute(
                        f"SELECT fact_id, content, summary, domain, owner_principal, confidence_score "
                        f"FROM facts WHERE status='active' AND owner_principal IN ({placeholders}) "
                        f"ORDER BY updated_at DESC LIMIT 20", owners,
                    ).fetchall()
                elif peers:
                    real_peers = [p for p in peers if p != "all"]
                    if real_peers:
                        placeholders = ",".join("?" for _ in real_peers)
                        rows = conn.execute(
                            f"SELECT fact_id, content, summary, domain, owner_principal, confidence_score "
                            f"FROM facts WHERE status='active' AND owner_principal IN ({placeholders}) "
                            f"ORDER BY updated_at DESC LIMIT 20", real_peers,
                        ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT fact_id, content, summary, domain, owner_principal, confidence_score "
                        "FROM facts WHERE status='active' ORDER BY updated_at DESC LIMIT 20",
                    ).fetchall()
            rows = [dict(r) for r in rows]
            rows = [
                dict(r) for r in rows
                if context.store.can_read(r["fact_id"], identity.principal_id,
                                          is_admin=identity.is_admin, roles=set(identity.roles))
            ]
            total = len(rows)
            summary = f"Federated {len(rows)} active fact(s) across {len(peers) or 'all'} peer scope(s)."
            with contextlib.suppress(Exception):
                context.store.write_audit(
                    actor=identity.principal_id, action="federation.query", target=peer_hierarchy,
                    payload={"peers": peers, "hits": total},
                )
        except Exception as e:  # noqa: BLE001
            summary = f"federation unavailable: {e}"
        return {"status": "ok", "peers": peers, "hierarchy": peer_hierarchy,
                "federation_config": "multi-agent-share", "summary": summary, "facts": rows,
                "count": total}

    # ── v11 symbolic short-term memory + code graph ──────────────────────
    @app.post("/v11/symbolic/offload")
    def symbolic_offload(body: dict,
                         identity: Principal = Depends(scoped("write"))):
        """Offload a heavy log/block into symbolic short-term memory."""
        from .symbolic_memory import SymbolicMemoryService
        svc = SymbolicMemoryService(context.store)
        session_key = body.get("session_key") or identity.principal_id
        owner = body.get("owner_principal") or identity.principal_id
        if not identity.can_act_as(owner):
            raise AuthError("cannot offload symbolic block for another principal", 403, "owner_boundary")
        block = svc.offload_block(
            session_key=session_key,
            raw_text=body.get("raw_text", ""),
            owner_principal=owner,
            block_type=body.get("block_type", "log"),
            parent_node_id=body.get("parent_node_id"),
            summary=body.get("summary"),
        )
        return {"status": "ok", "node_id": block.node_id,
                "block_id": block.block_id, "token_estimate": block.token_estimate,
                "mermaid": block.mermaid_line}

    @app.get("/v11/symbolic/canvas")
    def symbolic_canvas(session_key: str,
                        identity: Principal = Depends(scoped("read"))):
        """Return the Mermaid canvas for a session (drill-down map)."""
        from .symbolic_memory import SymbolicMemoryService
        svc = SymbolicMemoryService(context.store)
        blocks = svc.get_session_blocks(session_key)
        visible_blocks = [b for b in blocks if not b.get("owner_principal") or identity.is_admin or identity.can_act_as(b.get("owner_principal"))]
        if blocks and not visible_blocks and not identity.is_admin:
            raise AuthError("access denied to session blocks", 403, "owner_boundary")
        mermaid = svc.get_canvas(session_key)
        return {"status": "ok", "session_key": session_key,
                "mermaid": mermaid, "blocks": visible_blocks}

    @app.get("/v11/symbolic/{node_id}")
    def symbolic_recall(node_id: str,
                        identity: Principal = Depends(scoped("read"))):
        """Recall the full raw text for a symbol node_id."""
        from .symbolic_memory import SymbolicMemoryService
        svc = SymbolicMemoryService(context.store)
        block = svc.recall_block(node_id)
        if not block:
            raise HTTPException(404, "symbol node not found")
        owner = block.get("owner_principal")
        if owner and not identity.is_admin and not identity.can_act_as(owner):
            raise AuthError("access denied to symbol node", 403, "owner_boundary")
        return {"status": "ok", "node_id": node_id,
                "raw_text": block.get("raw_text"), "summary": block.get("summary"),
                "block_type": block.get("block_type")}

    @app.get("/v11/code/search")
    def code_search(q: str, limit: int = Query(default=20, ge=1, le=50),
                    identity: Principal = Depends(scoped("read"))):
        """Search indexed code symbols (CodeGraph)."""
        from .symbolic_memory import SymbolicMemoryService
        svc = SymbolicMemoryService(context.store)
        symbols = svc.search_code_symbols(q, limit=limit)
        return {"status": "ok", "query": q, "symbols": symbols, "count": len(symbols)}

    @app.get("/v11/code/impact/{symbol_id}")
    def code_impact(symbol_id: str,
                    identity: Principal = Depends(scoped("read"))):
        """Impact analysis: callers + callees of a code symbol."""
        from .symbolic_memory import SymbolicMemoryService
        svc = SymbolicMemoryService(context.store)
        impact = svc.get_code_impact(symbol_id)
        return {"status": "ok", **impact}

    # ── v12 EvolveMem: retrieval self-evolution ─────────────────────────
    @app.post("/v12/evolve/feedback", status_code=201)
    def evolve_feedback(body: dict,
                        identity: Principal = Depends(scoped("write"))):
        """Submit a retrieval quality signal (useful/useless/correction)."""
        from .evolve import EvolveMemService
        svc = EvolveMemService(context.store)
        query_text = (body or {}).get("query_text", "")
        fact_id = (body or {}).get("fact_id", "")
        signal = (body or {}).get("signal", "")
        if not query_text or not fact_id or not signal:
            raise HTTPException(422, "query_text, fact_id and signal are required")
        return svc.submit_feedback(query_text, fact_id, signal,
                                   user_principal=identity.principal_id,
                                   actor_principal=identity.principal_id)

    @app.get("/v12/evolve/report")
    def evolve_report(identity: Principal = Depends(scoped("read"))):
        """Retrieval quality dashboard (7-day window)."""
        from .evolve import EvolveMemService
        svc = EvolveMemService(context.store)
        return {"status": "ok", **svc.report()}

    @app.post("/v12/evolve/run")
    def evolve_run(identity: Principal = Depends(scoped("manage"))):
        """Trigger an evolution cycle now (aggregate feedback, nudge confidence)."""
        from .evolve import EvolveMemService
        svc = EvolveMemService(context.store)
        return svc.evolve(actor_principal=identity.principal_id)

    # ── v12 Conflict Resolution (M3a) ────────────────────────────────
    @app.post("/v12/conflicts/detect")
    def conflict_detect(threshold: float = 0.6,
                        identity: Principal = Depends(scoped("manage"))):
        """Scan active facts for near-duplicate contradiction pairs."""
        from .conflict import ConflictService
        if not (0.0 < threshold <= 1.0):
            raise HTTPException(422, "threshold must be in (0.0, 1.0]")
        svc = ConflictService(context.store)
        return svc.detect(threshold=threshold, actor_principal=identity.principal_id)

    @app.get("/v12/conflicts")
    def conflict_list(status: str = "open", limit: int = 50,
                      identity: Principal = Depends(scoped("read"))):
        """List conflict resolutions by status."""
        from .conflict import ConflictService
        svc = ConflictService(context.store)
        return {"status": "ok", "conflicts": svc.list(status=status, limit=limit)}

    @app.post("/v12/conflicts/{conflict_id}/resolve")
    def conflict_resolve(conflict_id: str, body: dict,
                         identity: Principal = Depends(scoped("manage"))):
        """Resolve a conflict: winner stays active, loser becomes disputed."""
        from .conflict import ConflictService, ConflictResolutionError
        winner = (body or {}).get("winner_fact_id", "")
        reason = (body or {}).get("reason", "")
        if not winner:
            raise HTTPException(422, "winner_fact_id is required")
        svc = ConflictService(context.store)
        try:
            return svc.resolve(conflict_id, winner, reason=reason,
                               actor_principal=identity.principal_id)
        except ConflictResolutionError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/v12/conflicts/{conflict_id}/dismiss")
    def conflict_dismiss(conflict_id: str, body: dict,
                         identity: Principal = Depends(scoped("manage"))):
        """Close a conflict without changing fact status."""
        from .conflict import ConflictService, ConflictResolutionError
        reason = (body or {}).get("reason", "")
        svc = ConflictService(context.store)
        try:
            return svc.dismiss(conflict_id, reason=reason,
                               actor_principal=identity.principal_id)
        except ConflictResolutionError as exc:
            raise HTTPException(409, str(exc))

    # ── v12 Skill Crystallization (M3b) ──────────────────────────────
    @app.post("/v12/crystals/scan")
    def crystal_scan(window_days: int = 7, min_freq: int = 3,
                     identity: Principal = Depends(scoped("manage"))):
        """Cluster recent facts by topic and surface crystallization candidates."""
        from .crystallize import CrystalService
        svc = CrystalService(context.store)
        try:
            return svc.scan(window_days=window_days, min_freq=min_freq,
                            actor_principal=identity.principal_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/v12/crystals")
    def crystal_list(status: str = "candidate", limit: int = 50,
                     identity: Principal = Depends(scoped("read"))):
        """List crystallization candidates by status."""
        from .crystallize import CrystalService
        svc = CrystalService(context.store)
        try:
            return {"status": "ok", "candidates": svc.list(status=status, limit=limit)}
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/v12/crystals/{candidate_id}/approve")
    def crystal_approve(candidate_id: str, body: dict | None = None,
                        identity: Principal = Depends(scoped("manage"))):
        """Human approval: materialize the crystallized skill as a pattern fact."""
        from .crystallize import CrystalError, CrystalService
        from .schema import AGENT_IDS
        owner = (body or {}).get("owner_principal") or identity.principal_id
        if owner not in AGENT_IDS:
            owner = "mentor"
        svc = CrystalService(context.store)
        try:
            return svc.approve(candidate_id, actor_principal=owner)
        except CrystalError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/v12/crystals/{candidate_id}/dismiss")
    def crystal_dismiss(candidate_id: str, body: dict,
                        identity: Principal = Depends(scoped("manage"))):
        """Reject a crystallization candidate without materializing anything."""
        from .crystallize import CrystalError, CrystalService
        reason = (body or {}).get("reason", "")
        svc = CrystalService(context.store)
        try:
            return svc.dismiss(candidate_id, reason=reason,
                               actor_principal=identity.principal_id)
        except CrystalError as exc:
            raise HTTPException(409, str(exc))

    # ── v12 Multi-modal assets (M4) ────────────────────────────────────
    @app.post("/v12/facts/{fact_id}/assets")
    def fact_asset_attach(fact_id: str, body: dict,
                          identity: Principal = Depends(scoped("manage"))):
        """Attach a multi-modal asset reference (image/audio/document/file)."""
        from .multimodal import AssetError, MultiModalService
        kind = (body or {}).get("asset_kind", "")
        ref = (body or {}).get("asset_ref", "")
        if not kind or not ref:
            raise HTTPException(422, "asset_kind and asset_ref are required")
        svc = MultiModalService(context.store)
        try:
            return svc.attach(fact_id, kind, ref,
                              actor_principal=identity.principal_id)
        except AssetError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/v12/facts/{fact_id}/assets")
    def fact_asset_list(fact_id: str,
                        identity: Principal = Depends(scoped("read"))):
        """List multi-modal assets attached to a fact."""
        from .multimodal import MultiModalService, asset_to_context
        svc = MultiModalService(context.store)
        return {"status": "ok", "fact_id": fact_id,
                "assets": asset_to_context(svc.list(fact_id))}

    @app.post("/v12/search/trace")
    def search_trace(body: QueryBody,
                     dedup_threshold: float = 0.8,
                     identity: Principal = Depends(scoped("read"))):
        """v12 recall funnel trace: candidate pool → gate → Jaccard dedup →
        Chronos decay → top-K, with per-stage timing/hits/decay factors."""
        if not (0.0 < dedup_threshold <= 1.0):
            raise HTTPException(422, "dedup_threshold must be in (0.0, 1.0]")
        result = context.query.trace(QueryRequest(
            text=body.text,
            principal_id=identity.principal_id,
            limit=body.limit,
            candidate_limit=body.candidate_limit,
            roles=tuple(identity.roles),
            is_admin=identity.is_admin,
            owner_principal=body.owner_principal,
            domain=body.domain,
            fact_type=body.fact_type,
            use_vector=body.use_vector,
            use_fts=body.use_fts,
            use_graph=body.use_graph,
            include_provisional=body.include_provisional,
        ), dedup_threshold=dedup_threshold)
        return {"status": "ok", **result}

    # Guarded v7 compatibility surface. It preserves client shapes but never
    # bypasses v8 canonical transactions or canonical ACL hydration.
    def query_v7(
        q: str,
        n: int = Query(default=5, ge=1, le=50),
        agent: str | None = None,
        domain: str | None = None,
        type: str | None = None,
        identity: Principal = Depends(scoped("read")),
    ):
        result = execute_query(QueryBody(
            text=q, limit=n, candidate_limit=max(50, n), owner_principal=agent,
            domain=domain, fact_type=type,
        ), identity)
        rows = []
        for item in result["results"]:
            row = dict(item)
            row["agent_id"] = row.pop("owner_principal")
            rows.append(row)
        return {"results": rows, "count": len(rows), "context_used": False,
                "cross_agent_used": False, "compatibility": "v7-on-v8"}

    @app.get("/stats")
    def stats_v7(identity: Principal = Depends(scoped("read"))):
        return {**visible_stats(identity), "compatibility": "v7-on-v8"}

    @app.get("/awareness")
    def awareness_v7(
        agent: str | None = None,
        hours: int | None = Query(default=None, ge=1, le=24 * 365),
        identity: Principal = Depends(scoped("read")),
    ):
        agent_id = agent or identity.principal_id
        updates = awareness_rows(identity, agent_id, hours)
        return {"updates": updates, "count": len(updates), "compatibility": "v7-on-v8"}

    @app.post("/write", status_code=201)
    def write_v7(body: dict, request: Request,
                 identity: Principal = Depends(scoped("write"))):
        required = [name for name in ("content", "agent", "domain", "type") if not body.get(name)]
        if required:
            raise ValidationError("missing required fields: " + ", ".join(required))
        owner = str(body["agent"])
        if not identity.can_act_as(owner):
            raise AuthError("cannot act as another agent", 403, "owner_boundary")
        if "skip_gatekeeper" in body:
            raise ValidationError("skip_gatekeeper is not allowed")
        result = context.store.create_fact(
            CreateFact(
                content=body["content"], summary=body.get("summary"),
                owner_principal=owner, domain=body["domain"], fact_type=body["type"],
                visibility=body.get("visibility", "all"),
                sensitivity=body.get("sensitivity", "internal"),
                egress_policy=body.get("egress_policy", "local_only"),
                project_id=body.get("project_id"), source_kind="v7_compat_api",
                idempotency_key=body.get("idempotency_key") or request.headers.get("Idempotency-Key"),
            ),
            actor_principal=identity.principal_id,
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
        )
        return {"status": "written", **result, "compatibility": "v7-on-v8"}

    @app.post("/delete")
    def delete_v7(body: dict, request: Request,
                  identity: Principal = Depends(scoped("write"))):
        fact_id = str(body.get("fact_id", "")).strip()
        if not fact_id:
            raise ValidationError("fact_id is required")
        fact = context.store.get_fact(fact_id)
        require_fact_permission(identity, fact, "delete")
        result = context.store.tombstone_fact(
            TombstoneFact(
                fact_id=fact_id,
                expected_version=int(body.get("expected_version", fact["current_version"])),
                reason=body.get("reason", "deleted through v7 compatibility API"),
                idempotency_key=body.get("idempotency_key") or request.headers.get("Idempotency-Key"),
            ),
            actor_principal=identity.principal_id,
            request_id=request.state.request_id,
            correlation_id=request.state.correlation_id,
        )
        return {"status": "deleted", **result, "compatibility": "v7-on-v8"}

    @app.get("/core_memory/{agent_id}/inject")
    def core_memory_v7_inject(
        agent_id: str,
        max_chars: int = Query(default=2000, ge=64, le=20_000),
        identity: Principal = Depends(scoped("read")),
    ):
        if not identity.can_act_as(agent_id):
            raise AuthError("core memory is owner-only", 403, "owner_boundary")
        if context.core_memory is None:
            raise HTTPException(503, "CoreMemory projection is not configured")
        text = context.core_memory.injection_text(agent_id, max_chars)
        return {"agent_id": agent_id, "injection_text": text,
                "compatibility": "v7-on-v8"}

    return app
