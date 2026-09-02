"""SQLite WAL canonical store for Mímir v8.

The store owns the transaction boundary. A fact mutation is committed together
with its immutable version, immutable event, audit record, and projector outbox.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .schema import (
    CreateFact,
    GrantFactAccess,
    PROJECTORS,
    SCHEMA_VERSION,
    DECAY_TIER_MAP,
    TombstoneFact,
    UpdateFact,
)


class ConflictError(RuntimeError):
    """Raised for idempotency or optimistic-version conflicts."""


class NotFoundError(LookupError):
    """Raised when a canonical fact does not exist."""


class SchemaVersionError(RuntimeError):
    """Raised when an existing database requires an explicit schema migration."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,
    legacy_id TEXT UNIQUE,
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    status TEXT NOT NULL CHECK (status IN ('active','tombstoned','disputed','archived')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    summary TEXT NOT NULL,
    domain TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    owner_principal TEXT NOT NULL,
    project_id TEXT,
    visibility TEXT NOT NULL CHECK (visibility IN ('all','shared','owner_only')),
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('internal','confidential','restricted')),
    egress_policy TEXT NOT NULL CHECK (egress_policy IN ('local_only','redacted_external','external_allowed')),
    human_status TEXT NOT NULL CHECK (human_status IN ('unreviewed','confirmed','rejected','disputed')),
    confidence_score REAL CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_verified_at TEXT,
    tombstoned_at TEXT,
    decay_tier TEXT NOT NULL DEFAULT 'L0_never' CHECK (decay_tier IN ('L0_never','L1_preference','L2_config','L3_event','L4_temporary','L5_ephemeral')),
    decayed_at TEXT,
    schema_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    CHECK (sensitivity != 'restricted' OR egress_policy = 'local_only')
) STRICT;

CREATE TABLE IF NOT EXISTS fact_versions (
    fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    version INTEGER NOT NULL CHECK (version >= 1),
    snapshot_json TEXT NOT NULL,
    change_type TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    actor_principal TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (fact_id, version)
) STRICT;

CREATE TABLE IF NOT EXISTS memory_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_principal TEXT NOT NULL,
    request_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    idempotency_key TEXT UNIQUE
) STRICT;

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_uri TEXT,
    content_hash TEXT,
    title TEXT,
    publisher TEXT,
    retrieved_at TEXT NOT NULL,
    license TEXT,
    trust_tier TEXT NOT NULL DEFAULT 'unknown'
) STRICT;

CREATE TABLE IF NOT EXISTS fact_sources (
    fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    version INTEGER NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    PRIMARY KEY (fact_id, version, source_id),
    FOREIGN KEY (fact_id, version) REFERENCES fact_versions(fact_id, version)
) STRICT;

CREATE TABLE IF NOT EXISTS relations (
    relation_id TEXT PRIMARY KEY,
    source_fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS resource_grants (
    grant_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    permission TEXT NOT NULL,
    effect TEXT NOT NULL CHECK (effect IN ('allow','deny')),
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    source_event_id TEXT NOT NULL,
    UNIQUE(resource_type, resource_id, subject_type, subject_id, permission, effect)
) STRICT;

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    actor_principal TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL REFERENCES memory_events(event_seq),
    projector_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','processing','done','retry','dead_letter')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    last_error_code TEXT,
    completed_at TEXT,
    UNIQUE(event_seq, projector_name)
) STRICT;

CREATE TABLE IF NOT EXISTS projector_state (
    projector_name TEXT PRIMARY KEY,
    checkpoint_event_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    last_error_code TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    vault_path TEXT NOT NULL UNIQUE,
    document_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','review','archived')),
    owner_principal TEXT NOT NULL,
    target_scope TEXT NOT NULL CHECK (target_scope IN ('private','shared')),
    current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    publication_hash TEXT,
    human_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS document_publications (
    publication_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    requested_revision INTEGER NOT NULL CHECK (requested_revision >= 1),
    expected_previous_hash TEXT,
    desired_hash TEXT NOT NULL,
    managed_sections_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('requested','published','conflict','failed')),
    requested_by TEXT NOT NULL,
    request_event_seq INTEGER NOT NULL REFERENCES memory_events(event_seq),
    published_event_seq INTEGER REFERENCES memory_events(event_seq),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS document_fact_references (
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    publication_id TEXT NOT NULL REFERENCES document_publications(publication_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (document_id, fact_id, publication_id)
) STRICT;

CREATE TABLE IF NOT EXISTS document_conflicts (
    conflict_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    publication_id TEXT REFERENCES document_publications(publication_id),
    conflict_type TEXT NOT NULL,
    expected_revision INTEGER,
    actual_revision INTEGER,
    expected_hash TEXT,
    actual_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('open','resolved')),
    detected_event_seq INTEGER NOT NULL REFERENCES memory_events(event_seq),
    resolved_event_seq INTEGER REFERENCES memory_events(event_seq),
    created_at TEXT NOT NULL,
    resolved_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS document_feedback (
    feedback_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    document_revision INTEGER NOT NULL,
    human_hash TEXT NOT NULL,
    feedback_text TEXT NOT NULL,
    feedback_hash TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_event_seq INTEGER NOT NULL REFERENCES memory_events(event_seq),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS candidate_facts (
    candidate_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN (
        'collected','parsed','extracted','deduplicated','review_required',
        'approved','committed','rejected','needs_more_evidence',
        'provisional','human_review','auto_rejected'
    )),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    summary TEXT NOT NULL,
    proposed_owner_principal TEXT NOT NULL,
    proposed_domain TEXT NOT NULL,
    proposed_fact_type TEXT NOT NULL,
    proposed_visibility TEXT NOT NULL,
    proposed_sensitivity TEXT NOT NULL,
    proposed_egress_policy TEXT NOT NULL,
    source_id TEXT REFERENCES sources(source_id),
    source_hash TEXT,
    confidence_score REAL CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
    uncertainty_json TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    reviewed_by TEXT,
    review_reason TEXT,
    committed_fact_id TEXT REFERENCES facts(fact_id),
    supersedes_fact_id TEXT REFERENCES facts(fact_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS review_actions (
    review_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_facts(candidate_id),
    action TEXT NOT NULL CHECK (action IN ('approve','reject','needs_more_evidence')),
    reason TEXT NOT NULL,
    reviewer_principal TEXT NOT NULL,
    event_seq INTEGER NOT NULL REFERENCES memory_events(event_seq),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS core_memory_items (
    item_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    block_name TEXT NOT NULL CHECK (block_name IN ('user_profile','project_context','key_decisions')),
    fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    fact_version INTEGER NOT NULL CHECK (fact_version >= 1),
    position INTEGER NOT NULL CHECK (position >= 0),
    status TEXT NOT NULL CHECK (status IN ('active','retired')),
    promoted_by TEXT NOT NULL,
    promotion_reason TEXT NOT NULL,
    promoted_event_seq INTEGER NOT NULL REFERENCES memory_events(event_seq),
    retired_by TEXT,
    retirement_reason TEXT,
    retired_event_seq INTEGER REFERENCES memory_events(event_seq),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_id, block_name, fact_id)
) STRICT;

CREATE TABLE IF NOT EXISTS conversation_sources (
    source_id TEXT PRIMARY KEY,
    connector_type TEXT NOT NULL CHECK (connector_type IN
        ('hermes_cdc','external_agent','workbuddy','file','rss','web','document','vault')),
    connector_id TEXT NOT NULL,
    session_id TEXT,
    source_uri TEXT,
    source_hash TEXT NOT NULL,
    title TEXT,
    owner_principal TEXT NOT NULL,
    retention_class TEXT NOT NULL CHECK (retention_class IN ('session','short','standard','permanent','legal_hold')),
    memory_mode TEXT NOT NULL CHECK (memory_mode IN ('explicit','observe','never')),
    source_category TEXT NOT NULL CHECK (source_category IN ('conversation','external_info','knowledge_doc','unknown/quarantine')),
    started_at TEXT,
    ended_at TEXT,
    ingested_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES conversation_sources(source_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool','developer','unknown')),
    principal_id TEXT,
    content_redacted TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    redaction_applied INTEGER NOT NULL CHECK (redaction_applied IN (0,1)),
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(source_id, ordinal)
) STRICT;

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES conversation_sources(source_id),
    status TEXT NOT NULL CHECK (status IN ('received','redacting','stored','extracted','failed','expired')),
    requested_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    redacted_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS extraction_runs (
    extraction_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    extractor_principal TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started','completed','failed','cancelled')),
    candidate_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS candidate_evidence (
    evidence_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_facts(candidate_id),
    source_id TEXT REFERENCES conversation_sources(source_id),
    message_id TEXT REFERENCES conversation_messages(message_id),
    quote_text_redacted TEXT NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    evidence_hash TEXT NOT NULL,
    added_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, evidence_hash)
) STRICT;

CREATE TABLE IF NOT EXISTS learning_policies (
    policy_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL UNIQUE,
    memory_mode TEXT NOT NULL CHECK (memory_mode IN ('explicit','observe','never')),
    retention_class TEXT NOT NULL CHECK (retention_class IN ('session','short','standard','permanent','legal_hold')),
    min_confidence REAL NOT NULL CHECK (min_confidence >= 0 AND min_confidence <= 1),
    requires_review INTEGER NOT NULL CHECK (requires_review IN (0,1)),
    dlp_profile TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS learning_feedback (
    feedback_id TEXT PRIMARY KEY,
    candidate_id TEXT REFERENCES candidate_facts(candidate_id),
    fact_id TEXT REFERENCES facts(fact_id),
    feedback_type TEXT NOT NULL CHECK (feedback_type IN ('useful','incorrect','stale','duplicate','harmful','withdraw')),
    feedback_text TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_seq INTEGER REFERENCES memory_events(event_seq),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_items (
    item_id TEXT PRIMARY KEY,
    layer TEXT NOT NULL CHECK (layer IN ('learning','wiki')),
    item_type TEXT NOT NULL CHECK (item_type IN ('learning_note','wiki_document')),
    status TEXT NOT NULL CHECK (status IN ('active','review','archived','quarantined')),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    summary TEXT NOT NULL,
    owner_principal TEXT NOT NULL,
    domain TEXT NOT NULL,
    topics_json TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('all','shared','owner_only')),
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('internal','confidential','restricted')),
    egress_policy TEXT NOT NULL CHECK (egress_policy IN ('local_only','redacted_external','external_allowed')),
    source_category TEXT NOT NULL CHECK (source_category IN ('external_info','knowledge_doc')),
    source_uri TEXT,
    source_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    stable_path TEXT,
    file_sha256 TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (layer != 'wiki' OR source_category = 'knowledge_doc'),
    CHECK (sensitivity != 'restricted' OR egress_policy = 'local_only'),
    UNIQUE(layer, owner_principal, source_hash, content_hash)
) STRICT;

CREATE TABLE IF NOT EXISTS knowledge_feedback_signals (
    signal_id TEXT PRIMARY KEY,
    target_layer TEXT NOT NULL CHECK (target_layer IN ('memory','learning','wiki')),
    target_id TEXT NOT NULL,
    signal_type TEXT NOT NULL CHECK (signal_type IN ('useful','incorrect','stale','duplicate','harmful','withdraw','explicit_reject','seen_pending')),
    signal_text TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    source_event_seq INTEGER REFERENCES memory_events(event_seq),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS governance_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    target_layer TEXT NOT NULL CHECK (target_layer IN ('memory','learning','wiki')),
    target_id TEXT NOT NULL,
    suggestion_type TEXT NOT NULL CHECK (suggestion_type IN ('candidate_review','remember','correct','forget','wiki_update')),
    status TEXT NOT NULL CHECK (status IN ('open','accepted','rejected','superseded')),
    rationale TEXT NOT NULL,
    created_from_signal_id TEXT REFERENCES knowledge_feedback_signals(signal_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS opinions (
    opinion_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    topic TEXT NOT NULL,
    stance TEXT NOT NULL CHECK (stance IN ('support','oppose','neutral')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    owner_principal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(fact_id, owner_principal)
) STRICT;

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    supporting_opinion_ids TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)),
    owner_principal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS search_feedback (
    feedback_id TEXT PRIMARY KEY,
    query_text TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    signal TEXT NOT NULL CHECK (signal IN ('useful', 'useless', 'correction')),
    user_principal TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_search_feedback_fact
ON search_feedback(fact_id);

CREATE INDEX IF NOT EXISTS idx_search_feedback_created
ON search_feedback(created_at);

CREATE TABLE IF NOT EXISTS quality_metrics (
    metric_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    query_count INTEGER NOT NULL DEFAULT 0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    avg_score REAL NOT NULL DEFAULT 0,
    zero_hit_count INTEGER NOT NULL DEFAULT 0,
    useful_signals INTEGER NOT NULL DEFAULT 0,
    useless_signals INTEGER NOT NULL DEFAULT 0,
    evolved_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_quality_metrics_date
ON quality_metrics(date);

CREATE TABLE IF NOT EXISTS conflict_resolutions (
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
) STRICT;

CREATE INDEX IF NOT EXISTS idx_conflict_status
ON conflict_resolutions(status, created_at);

CREATE INDEX IF NOT EXISTS idx_conflict_fact_a
ON conflict_resolutions(fact_id_a, status);

CREATE INDEX IF NOT EXISTS idx_conflict_fact_b
ON conflict_resolutions(fact_id_b, status);

CREATE TABLE IF NOT EXISTS crystal_candidates (
    candidate_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    domain TEXT NOT NULL,
    freq INTEGER NOT NULL CHECK (freq >= 1),
    sample_ids TEXT NOT NULL,
    suggestion TEXT,
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('candidate','approved','dismissed')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    crystal_fact_id TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_crystal_status
ON crystal_candidates(status, created_at);

CREATE INDEX IF NOT EXISTS idx_crystal_topic
ON crystal_candidates(topic, domain);

CREATE TABLE IF NOT EXISTS fact_assets (
    asset_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('image','audio','document','file')),
    asset_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor_principal TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_fact_assets_fact
ON fact_assets(fact_id, created_at);

CREATE TABLE IF NOT EXISTS retention_jobs (
    retention_job_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('conversation_source','conversation_message','candidate','fact')),
    resource_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled','held','executed','cancelled')),
    reason TEXT NOT NULL,
    legal_hold INTEGER NOT NULL CHECK (legal_hold IN (0,1)),
    requested_by TEXT NOT NULL,
    executed_by TEXT,
    event_seq INTEGER REFERENCES memory_events(event_seq),
    created_at TEXT NOT NULL,
    executed_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS connector_checkpoints (
    connector_id TEXT PRIMARY KEY,
    connector_type TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    source_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('active','paused','error')),
    updated_at TEXT NOT NULL,
    last_error_code TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_facts_owner_status ON facts(owner_principal, status);
CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(domain, fact_type, project_id, status);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON memory_events(aggregate_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(projector_name, status, event_seq);
CREATE INDEX IF NOT EXISTS idx_publications_status ON document_publications(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_publications_one_open_revision
ON document_publications(document_id, requested_revision)
WHERE status = 'requested';
CREATE INDEX IF NOT EXISTS idx_document_conflicts_status ON document_conflicts(status, created_at);
CREATE INDEX IF NOT EXISTS idx_document_feedback_document ON document_feedback(document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidate_facts(status, created_at);
CREATE INDEX IF NOT EXISTS idx_reviews_candidate ON review_actions(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_core_memory_agent_block
ON core_memory_items(agent_id, block_name, status, position, created_at);
CREATE INDEX IF NOT EXISTS idx_conversation_sources_connector
ON conversation_sources(connector_id, session_id, ingested_at);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_source
ON conversation_messages(source_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_status
ON ingestion_runs(status, started_at);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_status
ON extraction_runs(status, started_at);
CREATE INDEX IF NOT EXISTS idx_candidate_evidence_candidate
ON candidate_evidence(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_learning_feedback_fact
ON learning_feedback(fact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_items_search
ON knowledge_items(layer, status, domain, owner_principal, updated_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_items_source
ON knowledge_items(source_category, source_hash, updated_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_feedback_target
ON knowledge_feedback_signals(target_layer, target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_governance_suggestions_status
ON governance_suggestions(status, target_layer, created_at);
CREATE INDEX IF NOT EXISTS idx_retention_jobs_due
ON retention_jobs(status, due_at);

CREATE TRIGGER IF NOT EXISTS fact_versions_no_update
BEFORE UPDATE ON fact_versions BEGIN
    SELECT RAISE(ABORT, 'fact_versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS fact_versions_no_delete
BEFORE DELETE ON fact_versions BEGIN
    SELECT RAISE(ABORT, 'fact_versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_events_no_update
BEFORE UPDATE ON memory_events BEGIN
    SELECT RAISE(ABORT, 'memory_events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_events_no_delete
BEFORE DELETE ON memory_events BEGIN
    SELECT RAISE(ABORT, 'memory_events are immutable');
END;
"""


class CanonicalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._validate_existing_schema()
        else:
            self._initialize_fresh()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize_fresh(self) -> None:
        with contextlib.closing(self.connect()) as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            for projector in PROJECTORS:
                connection.execute(
                    "INSERT OR IGNORE INTO projector_state(projector_name, updated_at) VALUES(?, ?)",
                    (projector, utc_now()),
                )
            connection.commit()
            self._assert_required_schema(connection)

    def _validate_existing_schema(self) -> None:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if not has_meta:
                raise SchemaVersionError("database has no schema_meta; explicit migration is required")
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                raise SchemaVersionError("database has no schema_version; explicit migration is required")
            try:
                existing = int(row[0])
            except (TypeError, ValueError) as exc:
                raise SchemaVersionError(f"invalid database schema_version: {row[0]!r}") from exc
            if existing != SCHEMA_VERSION:
                direction = "newer" if existing > SCHEMA_VERSION else "older"
                raise SchemaVersionError(
                    f"database schema {existing} is {direction} than runtime schema {SCHEMA_VERSION}; "
                    "run an explicit migration or use a matching runtime"
                )
            self._assert_required_schema(connection)
        finally:
            connection.close()

    @staticmethod
    def _assert_required_schema(connection: sqlite3.Connection) -> None:
        required_tables = {
            "schema_meta", "facts", "fact_versions", "memory_events", "candidate_facts",
            "candidate_evidence", "conversation_sources", "conversation_messages",
            "ingestion_runs", "extraction_runs", "projector_state", "knowledge_items",
            "knowledge_feedback_signals", "governance_suggestions",
            "search_feedback", "quality_metrics", "conflict_resolutions",
            "crystal_candidates", "fact_assets",
        }
        present = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = sorted(required_tables - present)
        if missing:
            raise SchemaVersionError(f"database schema is incomplete; missing tables: {missing}")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversation_sources)")
        }
        if "source_category" not in columns:
            raise SchemaVersionError(
                "database schema is incomplete; conversation_sources.source_category is missing"
            )

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_fact(
        self,
        command: CreateFact,
        actor_principal: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
        failure_hook: Callable[[str], None] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict:
        cmd = command.validated()
        request_id = request_id or new_id()
        correlation_id = correlation_id or request_id
        now = utc_now()
        recorded_at = cmd.recorded_at or now
        fact_id = new_id()
        event_id = new_id()
        content_hash = sha256_text(cmd.content)
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    key: value
                    for key, value in asdict(cmd).items()
                    if key != "idempotency_key"
                }
            )
        )
        event_payload = {
            "fact_id": fact_id,
            "legacy_id": cmd.legacy_id,
            "version": 1,
            "content_hash": content_hash,
            "request_fingerprint": request_fingerprint,
            "owner_principal": cmd.owner_principal,
            "domain": cmd.domain,
            "fact_type": cmd.fact_type,
            "visibility": cmd.visibility,
            "sensitivity": cmd.sensitivity,
            "egress_policy": cmd.egress_policy,
            "project_id": cmd.project_id,
        }
        snapshot = {
            **asdict(cmd),
            "fact_id": fact_id,
            "current_version": 1,
            "status": "active",
            "recorded_at": recorded_at,
            "updated_at": now,
            "last_verified_at": cmd.last_verified_at,
            "schema_version": SCHEMA_VERSION,
            "content_hash": content_hash,
        }

        if connection is None:
            with self.transaction() as conn:
                return self._create_fact_on(
                    connection=conn,
                    cmd=cmd, actor_principal=actor_principal,
                    request_id=request_id, correlation_id=correlation_id,
                    failure_hook=failure_hook, now=now, recorded_at=recorded_at,
                    fact_id=fact_id, event_id=event_id, content_hash=content_hash,
                    request_fingerprint=request_fingerprint, event_payload=event_payload,
                    snapshot=snapshot,
                )
        return self._create_fact_on(
            connection=connection,
            cmd=cmd, actor_principal=actor_principal,
            request_id=request_id, correlation_id=correlation_id,
            failure_hook=failure_hook, now=now, recorded_at=recorded_at,
            fact_id=fact_id, event_id=event_id, content_hash=content_hash,
            request_fingerprint=request_fingerprint, event_payload=event_payload,
            snapshot=snapshot,
        )

    def _create_fact_on(
        self,
        *,
        connection: sqlite3.Connection,
        cmd: CreateFact,
        actor_principal: str,
        request_id: str,
        correlation_id: str,
        failure_hook: Callable[[str], None] | None,
        now: str,
        recorded_at: str,
        fact_id: str,
        event_id: str,
        content_hash: str,
        request_fingerprint: str,
        event_payload: dict,
        snapshot: dict,
    ) -> dict:
        if cmd.idempotency_key:
            existing = connection.execute(
                "SELECT event_id, aggregate_id, payload_json FROM memory_events WHERE idempotency_key=?",
                (cmd.idempotency_key,),
            ).fetchone()
            if existing:
                existing_payload = json.loads(existing["payload_json"])
                if existing_payload.get("request_fingerprint") != request_fingerprint:
                    raise ConflictError("idempotency key was reused with different content")
                return {
                    "fact_id": existing["aggregate_id"],
                    "event_id": existing["event_id"],
                    "idempotent_replay": True,
                }

        connection.execute(
            """INSERT INTO facts(
                fact_id, legacy_id, current_version, status, content, summary,
                domain, fact_type, owner_principal, project_id, visibility,
                sensitivity, egress_policy, human_status, confidence_score,
                valid_from, valid_to, recorded_at, updated_at, last_verified_at,
                schema_version, content_hash, decay_tier
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fact_id,
                cmd.legacy_id,
                1,
                "active",
                cmd.content,
                cmd.summary,
                cmd.domain,
                cmd.fact_type,
                cmd.owner_principal,
                cmd.project_id,
                cmd.visibility,
                cmd.sensitivity,
                cmd.egress_policy,
                cmd.human_status,
                cmd.confidence_score,
                cmd.valid_from,
                cmd.valid_to,
                recorded_at,
                now,
                cmd.last_verified_at,
                SCHEMA_VERSION,
                content_hash,
                DECAY_TIER_MAP.get(cmd.fact_type, "L4_temporary"),
            ),
        )
        if failure_hook:
            failure_hook("after_fact")

        payload_json = canonical_json(event_payload)
        payload_hash = sha256_text(payload_json)
        cursor = connection.execute(
            """INSERT INTO memory_events(
                event_id, aggregate_type, aggregate_id, aggregate_version,
                event_type, actor_principal, request_id, correlation_id,
                occurred_at, payload_json, payload_hash, idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                "fact",
                fact_id,
                1,
                cmd.event_type,
                actor_principal,
                request_id,
                correlation_id,
                now,
                payload_json,
                payload_hash,
                cmd.idempotency_key,
            ),
        )
        event_seq = int(cursor.lastrowid)
        if failure_hook:
            failure_hook("after_event")

        connection.execute(
            """INSERT INTO fact_versions(
                fact_id, version, snapshot_json, change_type, change_reason,
                actor_principal, source_event_id, created_at, content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                fact_id,
                1,
                canonical_json(snapshot),
                cmd.event_type,
                "initial canonical version",
                actor_principal,
                event_id,
                now,
                content_hash,
            ),
        )

        source_id = None
        if cmd.source_kind:
            source_id = new_id()
            connection.execute(
                """INSERT INTO sources(
                    source_id, source_kind, source_uri, content_hash, retrieved_at
                ) VALUES(?,?,?,?,?)""",
                (source_id, cmd.source_kind, cmd.source_uri, cmd.source_hash, now),
            )
            connection.execute(
                "INSERT INTO fact_sources(fact_id, version, source_id) VALUES(?,?,?)",
                (fact_id, 1, source_id),
            )

        self._create_default_grants(
            connection, fact_id, cmd.owner_principal, cmd.visibility, actor_principal, event_id, now
        )
        connection.execute(
            """INSERT INTO audit_log(
                audit_id, occurred_at, actor_principal, action, resource_type,
                resource_id, request_id, outcome, detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                new_id(),
                now,
                actor_principal,
                cmd.event_type,
                "fact",
                fact_id,
                request_id,
                "success",
                canonical_json({"event_id": event_id, "content_hash": content_hash}),
            ),
        )
        for projector in PROJECTORS:
            connection.execute(
                """INSERT INTO outbox(
                    outbox_id, event_seq, projector_name, status, available_at
                ) VALUES(?,?,?,?,?)""",
                (new_id(), event_seq, projector, "pending", now),
            )
        if failure_hook:
            failure_hook("before_commit")

        return {
            "fact_id": fact_id,
            "event_id": event_id,
            "event_seq": event_seq,
            "version": 1,
            "source_id": source_id,
            "projection_status": "pending",
            "idempotent_replay": False,
        }

    @staticmethod
    def _create_default_grants(
        connection: sqlite3.Connection,
        fact_id: str,
        owner: str,
        visibility: str,
        actor: str,
        event_id: str,
        now: str,
    ) -> None:
        grants = [("principal", owner, "read", "allow"), ("principal", owner, "write", "allow")]
        grants.append(("role", "admin", "manage", "allow"))
        if visibility == "all":
            grants.append(("role", "authenticated", "read", "allow"))
        elif visibility == "shared":
            grants.append(("role", "federated_agents", "read", "allow"))
        for subject_type, subject_id, permission, effect in grants:
            connection.execute(
                """INSERT INTO resource_grants(
                    grant_id, resource_type, resource_id, subject_type, subject_id,
                    permission, effect, granted_by, created_at, source_event_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id(),
                    "fact",
                    fact_id,
                    subject_type,
                    subject_id,
                    permission,
                    effect,
                    actor,
                    now,
                    event_id,
                ),
            )

    def update_fact(
        self,
        command: UpdateFact,
        actor_principal: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        cmd = command.validated()
        request_id = request_id or new_id()
        correlation_id = correlation_id or request_id
        now = utc_now()
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    key: value
                    for key, value in asdict(cmd).items()
                    if key != "idempotency_key"
                }
            )
        )
        with self.transaction() as connection:
            replay = self._idempotent_result(
                connection, cmd.idempotency_key, request_fingerprint
            )
            if replay:
                return replay
            current = connection.execute(
                "SELECT * FROM facts WHERE fact_id=?", (cmd.fact_id,)
            ).fetchone()
            if not current:
                raise NotFoundError(cmd.fact_id)
            if current["current_version"] != cmd.expected_version:
                raise ConflictError(
                    f"expected version {cmd.expected_version}, current version is {current['current_version']}"
                )
            if current["status"] == "tombstoned":
                raise ConflictError("tombstoned facts must be restored before update")

            new_version = int(current["current_version"]) + 1
            content = cmd.content if cmd.content is not None else current["content"]
            summary = cmd.summary if cmd.summary is not None else current["summary"]
            human_status = (
                cmd.human_status if cmd.human_status is not None else current["human_status"]
            )
            confidence = (
                cmd.confidence_score
                if cmd.confidence_score is not None
                else current["confidence_score"]
            )
            valid_from = cmd.valid_from if cmd.valid_from is not None else current["valid_from"]
            valid_to = cmd.valid_to if cmd.valid_to is not None else current["valid_to"]
            content_hash = sha256_text(content)
            changed_fields = [
                name
                for name, before, after in (
                    ("content", current["content"], content),
                    ("summary", current["summary"], summary),
                    ("human_status", current["human_status"], human_status),
                    ("confidence_score", current["confidence_score"], confidence),
                    ("valid_from", current["valid_from"], valid_from),
                    ("valid_to", current["valid_to"], valid_to),
                )
                if before != after
            ]
            if not changed_fields:
                raise ConflictError("update does not change canonical state")
            event_id = new_id()
            payload = {
                "fact_id": cmd.fact_id,
                "version": new_version,
                "request_fingerprint": request_fingerprint,
                "changed_fields": changed_fields,
                "content_hash": content_hash,
            }
            event_seq = self._insert_event(
                connection,
                event_id=event_id,
                fact_id=cmd.fact_id,
                version=new_version,
                event_type="fact.updated",
                actor_principal=actor_principal,
                request_id=request_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload=payload,
                idempotency_key=cmd.idempotency_key,
            )
            connection.execute(
                """UPDATE facts SET current_version=?, content=?, summary=?,
                human_status=?, confidence_score=?, valid_from=?, valid_to=?,
                updated_at=?, content_hash=? WHERE fact_id=?""",
                (
                    new_version,
                    content,
                    summary,
                    human_status,
                    confidence,
                    valid_from,
                    valid_to,
                    now,
                    content_hash,
                    cmd.fact_id,),
            )
            snapshot = dict(current)
            snapshot.update(
                {
                    "current_version": new_version,
                    "content": content,
                    "summary": summary,
                    "human_status": human_status,
                    "confidence_score": confidence,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "updated_at": now,
                    "content_hash": content_hash,
                }
            )
            self._insert_version_and_side_effects(
                connection,
                fact_id=cmd.fact_id,
                version=new_version,
                snapshot=snapshot,
                change_type="fact.updated",
                change_reason=cmd.change_reason,
                actor_principal=actor_principal,
                event_id=event_id,
                event_seq=event_seq,
                request_id=request_id,
                now=now,
                content_hash=content_hash,
            )
        return {
            "fact_id": cmd.fact_id,
            "event_id": event_id,
            "event_seq": event_seq,
            "version": new_version,
            "projection_status": "pending",
            "idempotent_replay": False,
        }

    def tombstone_fact(
        self,
        command: TombstoneFact,
        actor_principal: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        cmd = command.validated()
        request_id = request_id or new_id()
        correlation_id = correlation_id or request_id
        now = utc_now()
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    key: value
                    for key, value in asdict(cmd).items()
                    if key != "idempotency_key"
                }
            )
        )
        with self.transaction() as connection:
            replay = self._idempotent_result(
                connection, cmd.idempotency_key, request_fingerprint
            )
            if replay:
                return replay
            current = connection.execute(
                "SELECT * FROM facts WHERE fact_id=?", (cmd.fact_id,)
            ).fetchone()
            if not current:
                raise NotFoundError(cmd.fact_id)
            if current["current_version"] != cmd.expected_version:
                raise ConflictError(
                    f"expected version {cmd.expected_version}, current version is {current['current_version']}"
                )
            if current["status"] == "tombstoned":
                raise ConflictError("fact is already tombstoned")
            new_version = int(current["current_version"]) + 1
            event_id = new_id()
            payload = {
                "fact_id": cmd.fact_id,
                "version": new_version,
                "request_fingerprint": request_fingerprint,
                "reason": cmd.reason,
                "content_hash": current["content_hash"],
            }
            event_seq = self._insert_event(
                connection,
                event_id=event_id,
                fact_id=cmd.fact_id,
                version=new_version,
                event_type="fact.tombstoned",
                actor_principal=actor_principal,
                request_id=request_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload=payload,
                idempotency_key=cmd.idempotency_key,
            )
            connection.execute(
                """UPDATE facts SET current_version=?, status='tombstoned',
                tombstoned_at=?, updated_at=? WHERE fact_id=?""",
                (new_version, now, now, cmd.fact_id),
            )
            snapshot = dict(current)
            snapshot.update(
                {
                    "current_version": new_version,
                    "status": "tombstoned",
                    "tombstoned_at": now,
                    "updated_at": now,
                }
            )
            self._insert_version_and_side_effects(
                connection,
                fact_id=cmd.fact_id,
                version=new_version,
                snapshot=snapshot,
                change_type="fact.tombstoned",
                change_reason=cmd.reason,
                actor_principal=actor_principal,
                event_id=event_id,
                event_seq=event_seq,
                request_id=request_id,
                now=now,
                content_hash=current["content_hash"],
            )
        return {
            "fact_id": cmd.fact_id,
            "event_id": event_id,
            "event_seq": event_seq,
            "version": new_version,
            "projection_status": "pending",
            "idempotent_replay": False,
        }

    @staticmethod
    def _idempotent_result(
        connection: sqlite3.Connection,
        idempotency_key: str | None,
        request_fingerprint: str,
    ) -> dict | None:
        if not idempotency_key:
            return None
        existing = connection.execute(
            """SELECT event_id, event_seq, aggregate_id, aggregate_version, payload_json
            FROM memory_events WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
        if not existing:
            return None
        payload = json.loads(existing["payload_json"])
        if payload.get("request_fingerprint") != request_fingerprint:
            raise ConflictError("idempotency key was reused with different content")
        return {
            "fact_id": existing["aggregate_id"],
            "event_id": existing["event_id"],
            "event_seq": existing["event_seq"],
            "version": existing["aggregate_version"],
            "projection_status": "pending",
            "idempotent_replay": True,
        }

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        fact_id: str,
        version: int,
        event_type: str,
        actor_principal: str,
        request_id: str,
        correlation_id: str,
        occurred_at: str,
        payload: dict,
        idempotency_key: str | None,
    ) -> int:
        payload_json = canonical_json(payload)
        cursor = connection.execute(
            """INSERT INTO memory_events(
                event_id, aggregate_type, aggregate_id, aggregate_version,
                event_type, actor_principal, request_id, correlation_id,
                occurred_at, payload_json, payload_hash, idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                "fact",
                fact_id,
                version,
                event_type,
                actor_principal,
                request_id,
                correlation_id,
                occurred_at,
                payload_json,
                sha256_text(payload_json),
                idempotency_key,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_version_and_side_effects(
        connection: sqlite3.Connection,
        *,
        fact_id: str,
        version: int,
        snapshot: dict,
        change_type: str,
        change_reason: str,
        actor_principal: str,
        event_id: str,
        event_seq: int,
        request_id: str,
        now: str,
        content_hash: str,
    ) -> None:
        connection.execute(
            """INSERT INTO fact_versions(
                fact_id, version, snapshot_json, change_type, change_reason,
                actor_principal, source_event_id, created_at, content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                fact_id,
                version,
                canonical_json(snapshot),
                change_type,
                change_reason,
                actor_principal,
                event_id,
                now,
                content_hash,),
        )
        connection.execute(
            """INSERT INTO audit_log(
                audit_id, occurred_at, actor_principal, action, resource_type,
                resource_id, request_id, outcome, detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                new_id(),
                now,
                actor_principal,
                change_type,
                "fact",
                fact_id,
                request_id,
                "success",
                canonical_json({"event_id": event_id, "version": version}),
            ),
        )
        for projector in PROJECTORS:
            connection.execute(
                """INSERT INTO outbox(
                    outbox_id, event_seq, projector_name, status, available_at
                ) VALUES(?,?,?,?,?)""",
                (new_id(), event_seq, projector, "pending", now),
            )

    def grant_fact_access(
        self,
        command: GrantFactAccess,
        actor_principal: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        cmd = command.validated()
        request_id = request_id or new_id()
        correlation_id = correlation_id or request_id
        now = utc_now()
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    key: value
                    for key, value in asdict(cmd).items()
                    if key != "idempotency_key"
                }
            )
        )
        with self.transaction() as connection:
            replay = self._idempotent_result(
                connection, cmd.idempotency_key, request_fingerprint
            )
            if replay:
                return replay
            fact = connection.execute(
                "SELECT current_version FROM facts WHERE fact_id=?", (cmd.fact_id,)
            ).fetchone()
            if not fact:
                raise NotFoundError(cmd.fact_id)
            event_id = new_id()
            payload = {
                "fact_id": cmd.fact_id,
                "version": fact["current_version"],
                "request_fingerprint": request_fingerprint,
                "subject_type": cmd.subject_type,
                "subject_id": cmd.subject_id,
                "permission": cmd.permission,
                "effect": cmd.effect,
                "expires_at": cmd.expires_at,
            }
            event_type = "acl.granted" if cmd.effect == "allow" else "acl.revoked"
            event_seq = self._insert_event(
                connection,
                event_id=event_id,
                fact_id=cmd.fact_id,
                version=fact["current_version"],
                event_type=event_type,
                actor_principal=actor_principal,
                request_id=request_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload=payload,
                idempotency_key=cmd.idempotency_key,
            )
            connection.execute(
                """INSERT INTO resource_grants(
                    grant_id, resource_type, resource_id, subject_type, subject_id,
                    permission, effect, granted_by, created_at, expires_at, source_event_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(resource_type, resource_id, subject_type, subject_id, permission, effect)
                DO UPDATE SET expires_at=excluded.expires_at, granted_by=excluded.granted_by,
                created_at=excluded.created_at, source_event_id=excluded.source_event_id""",
                (
                    new_id(),
                    "fact",
                    cmd.fact_id,
                    cmd.subject_type,
                    cmd.subject_id,
                    cmd.permission,
                    cmd.effect,
                    actor_principal,
                    now,
                    cmd.expires_at,
                    event_id,
                ),
            )
            connection.execute(
                """INSERT INTO audit_log(
                    audit_id, occurred_at, actor_principal, action, resource_type,
                    resource_id, request_id, outcome, detail_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id(),
                    now,
                    actor_principal,
                    event_type,
                    "fact",
                    cmd.fact_id,
                    request_id,
                    "success",
                    canonical_json(payload),
                ),
            )
            connection.execute(
                """INSERT INTO outbox(
                    outbox_id, event_seq, projector_name, status, available_at
                ) VALUES(?,?,?,?,?)""",
                (new_id(), event_seq, "core_memory", "pending", now),
            )
        return {
            "fact_id": cmd.fact_id,
            "event_id": event_id,
            "event_seq": event_seq,
            "version": fact["current_version"],
            "projection_status": "not_required",
            "idempotent_replay": False,
        }

    def can_access(
        self,
        fact_id: str,
        principal_id: str,
        permission: str,
        *,
        is_admin: bool = False,
        roles: set[str] | None = None,
    ) -> bool:
        if permission not in {"read", "write", "review", "delete", "export", "manage"}:
            raise ValueError(f"invalid permission: {permission}")
        if is_admin:
            return True
        roles = set(roles or ()) | {"authenticated"}
        with contextlib.closing(self.connect()) as connection:
            fact = connection.execute(
                "SELECT owner_principal, status FROM facts WHERE fact_id=?", (fact_id,)
            ).fetchone()
            if not fact or fact["status"] == "tombstoned":
                return False
            owner_matches = fact["owner_principal"] == principal_id
            rows = connection.execute(
                """SELECT subject_type, subject_id, effect FROM resource_grants
                WHERE resource_type='fact' AND resource_id=? AND permission IN (?, 'manage')
                AND (expires_at IS NULL OR expires_at > ?)""",
                (fact_id, permission, utc_now()),
            ).fetchall()
        matched = []
        for row in rows:
            if row["subject_type"] == "principal" and row["subject_id"] == principal_id:
                matched.append(row["effect"])
            elif row["subject_type"] == "role" and row["subject_id"] in roles:
                matched.append(row["effect"])
        if "deny" in matched:
            return False
        owner_default = owner_matches and permission in {"read", "write", "delete"}
        return owner_default or "allow" in matched

    def can_read(
        self,
        fact_id: str,
        principal_id: str,
        *,
        is_admin: bool = False,
        roles: set[str] | None = None,
    ) -> bool:
        return self.can_access(
            fact_id, principal_id, "read", is_admin=is_admin, roles=roles
        )

    def get_fact(self, fact_id: str) -> dict:
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            if not row:
                raise NotFoundError(fact_id)
            return dict(row)

    def counts(self) -> dict:
        with contextlib.closing(self.connect()) as connection:
            names = (
                "facts",
                "fact_versions",
                "memory_events",
                "sources",
                "relations",
                "resource_grants",
                "audit_log",
                "outbox",
            )
            return {
                name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in names
            }

    def event_head(self) -> int:
        with contextlib.closing(self.connect()) as connection:
            return int(connection.execute("SELECT COALESCE(MAX(event_seq), 0) FROM memory_events").fetchone()[0])

    def write_audit(self, actor: str, action: str, target: str,
                    payload: dict | None = None, *, outcome: str = "success",
                    request_id: str | None = None) -> None:
        """记录一次性审计事件 (无事件溯源, 仅 audit_log 留痕)."""
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO audit_log(
                    audit_id, occurred_at, actor_principal, action, resource_type,
                    resource_id, request_id, outcome, detail_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id(), utc_now(), actor, action, "v10",
                    target, request_id or new_id(), outcome,
                    canonical_json(payload or {}),
                ),
            )
