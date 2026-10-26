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
import urllib.error
import urllib.request

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


def load_sources_yaml(path: str | Path | None = None) -> dict:
    """加载并验证 `sources.yaml` 信源登记册。

    v1.6.0 第二步：声明式探针与信源调度引擎的配置入口。

    - ``path`` 为空时使用包内默认 `mimir_v8/sources.yaml`。
    - 返回的 dict 保证含 ``models`` / ``github`` / ``tools`` 三个键（缺省为空列表），
      调用方无需逐个 ``get`` 防御。
    - 文件缺失或 YAML 非法时显式抛错（不静默回退空配置——坏配置静默等于没接线）。
    """
    import yaml

    if path is None:
        path = Path(__file__).resolve().parent / "sources.yaml"
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"sources.yaml not found at {p}")

    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"sources.yaml must be a mapping at top level, got {type(data).__name__}")

    # 归一化三个声明段，确保下游无需防御性 get
    for key in ("models", "github", "tools"):
        section = data.get(key)
        if section is None:
            data[key] = []
        elif not isinstance(section, list):
            raise ValueError(f"sources.yaml section '{key}' must be a list, got {type(section).__name__}")
    return data


def _lineage_hash_for_new_version(
    connection: sqlite3.Connection, fact_id: str, version: int
) -> str:
    """谱系链值取法（1.1.0）：委托 mimir_v8.lineage，局部导入避免循环依赖。"""
    from .lineage import lineage_hash_for_new_version

    return lineage_hash_for_new_version(connection, fact_id, version)


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
    previous_version_hash TEXT,
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

-- relations: v13.0 TKG adds valid_from/valid_until ('' = open
-- interval, also the additive default for pre-existing rows). Keep
-- comments OUT of the column list: SQLite stores the DDL verbatim and
-- DROP COLUMN (used to simulate old schemas in tests/migrations) cannot
-- reparse a table whose comments dangle after a dropped column.
CREATE TABLE IF NOT EXISTS relations (
    relation_id TEXT PRIMARY KEY,
    source_fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT ''
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
    connector_type TEXT NOT NULL,
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

-- v10 governance audit trail. These two tables existed only in the production
-- database (created ad hoc by the retired dashboard/governance.py); a fresh
-- install had no DDL for them, so governance INSERTs failed inside a broad
-- except and vanished (audit 2026-09-07). DDL mirrors production verbatim
-- (non-STRICT, defaults included) so IF NOT EXISTS is a no-op there.
CREATE TABLE IF NOT EXISTS candidate_review_assessments (
    assessment_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    reviewer_type TEXT NOT NULL DEFAULT 'rule',
    provider TEXT,
    model TEXT,
    recommendation TEXT,
    risk TEXT,
    confidence REAL,
    is_valuable INTEGER,
    is_noise INTEGER,
    domain TEXT,
    fact_type TEXT,
    summary TEXT,
    reasoning TEXT,
    raw_output_hash TEXT,
    success INTEGER NOT NULL DEFAULT 1,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_assessments_candidate
ON candidate_review_assessments(candidate_id);

CREATE TABLE IF NOT EXISTS governance_decisions (
    decision_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    policy_version TEXT NOT NULL DEFAULT 'v9.1-r1',
    assessment_id TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    automatic INTEGER NOT NULL DEFAULT 1,
    actor_principal TEXT NOT NULL DEFAULT 'service:governance',
    previous_status TEXT,
    new_status TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_decisions_candidate
ON governance_decisions(candidate_id);

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

CREATE TABLE IF NOT EXISTS crystal_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('started','completed','failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT,
    window_days INTEGER,
    created INTEGER,
    updated INTEGER,
    skipped INTEGER
) STRICT;

CREATE INDEX IF NOT EXISTS idx_crystal_runs_status
ON crystal_runs(status, started_at);

CREATE TABLE IF NOT EXISTS tombstone_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    owner_principal TEXT NOT NULL,
    domain TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    visibility TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    egress_policy TEXT NOT NULL,
    confidence_score REAL,
    decay_tier TEXT,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT,
    updated_at TEXT,
    version_at_tombstone INTEGER NOT NULL,
    tombstoned_by TEXT NOT NULL,
    tombstoned_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_tombstone_snapshots_expiry
ON tombstone_snapshots(expires_at);

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

-- v1.6.0: entity knowledge base (additive extension of the v21 schema;
-- present in the fresh schema and reached by older databases via
-- migrate_schema_21_to_22).
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT,
    metadata_json TEXT,
    sensitivity TEXT DEFAULT 'internal',
    visibility TEXT DEFAULT 'all',
    status TEXT DEFAULT 'active',
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_hash TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    role TEXT,
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (fact_id, entity_id),
    FOREIGN KEY (fact_id) REFERENCES facts(fact_id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE IF NOT EXISTS entity_relations (
    from_entity_id TEXT NOT NULL,
    to_entity_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    relation_direction TEXT DEFAULT 'forward',
    strength REAL DEFAULT 1.0,
    metadata_json TEXT,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_entity_id, to_entity_id, relation_type),
    FOREIGN KEY (from_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX IF NOT EXISTS idx_entities_domain ON entities(domain);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);
CREATE INDEX IF NOT EXISTS idx_entities_valid ON entities(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_fact_entities_fact ON fact_entities(fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_fact_entities_role ON fact_entities(role);
CREATE INDEX IF NOT EXISTS idx_entity_relations_from ON entity_relations(from_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_relations_to ON entity_relations(to_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_relations_type ON entity_relations(relation_type);

-- v1.6.0 Step 2: debt-replay queue for failed model probes.
CREATE TABLE IF NOT EXISTS probe_tasks (
    task_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    due_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_probe_tasks_due ON probe_tasks(due_at, status);
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
                actor_principal, source_event_id, created_at, content_hash,
                previous_version_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
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
                "",  # genesis：链首无前驱
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
            decay_tier = (
                cmd.decay_tier if cmd.decay_tier is not None else current["decay_tier"]
            )
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
                    ("decay_tier", current["decay_tier"], decay_tier),
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
                decay_tier=?, updated_at=?, content_hash=? WHERE fact_id=?""",
                (
                    new_version,
                    content,
                    summary,
                    human_status,
                    confidence,
                    valid_from,
                    valid_to,
                    decay_tier,
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
                    "decay_tier": decay_tier,
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

    def restore_fact(
        self,
        fact_id: str,
        *,
        actor_principal: str,
        reason: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict:
        """1.0-C1: tombstone 恢复——快照回插 + fact.restored 事件。

        「可悔」的操作腿：30 天窗口内把 tombstone 前完整状态回插为
        active。幂等：已 active 的事实重复 restore 返回现态不重复加版。
        事件链 tombstoned → restored 全程可审计。
        """
        request_id = request_id or new_id()
        correlation_id = correlation_id or request_id
        now = utc_now()
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT fact_id, status, current_version FROM facts WHERE fact_id=?",
                (fact_id,),
            ).fetchone()
            if not current:
                raise NotFoundError(fact_id)
            if current["status"] == "active":
                return {"fact_id": fact_id, "status": "active",
                        "restored": False, "reason": "already active"}
            snap = connection.execute(
                "SELECT * FROM tombstone_snapshots WHERE fact_id=?"
                " ORDER BY tombstoned_at DESC LIMIT 1", (fact_id,),
            ).fetchone()
            if snap is None:
                raise ConflictError(
                    f"no tombstone snapshot for {fact_id} — "
                    "restore window expired or snapshot missing")
            new_version = int(current["current_version"]) + 1
            event_id = new_id()
            payload = {
                "fact_id": fact_id,
                "version": new_version,
                "reason": reason,
                "restored_from_snapshot_at": snap["tombstoned_at"],
            }
            self._insert_event(
                connection,
                event_id=event_id,
                fact_id=fact_id,
                version=new_version,
                event_type="fact.restored",
                actor_principal=actor_principal,
                request_id=request_id,
                correlation_id=correlation_id,
                occurred_at=now,
                payload=payload,
                idempotency_key=f"restore-{event_id}",
            )
            connection.execute(
                """UPDATE facts SET current_version=?, status='active',
                tombstoned_at=NULL, updated_at=? WHERE fact_id=?""",
                (new_version, now, fact_id),
            )
            # 快照保留（审计链），garbage 由 ops 30 天窗口清理
            return {"fact_id": fact_id, "status": "active",
                    "restored": True, "reason": reason}

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
            # 1.0-C1: tombstone 快照与状态翻转同事务——「可悔」从事件账本
            # 升级为可操作恢复路径；30 天窗口内 restore_fact 可完整回插。
            _snapshot_days = 30
            from datetime import timedelta as _td
            _expires = (datetime.fromisoformat(now) + _td(days=_snapshot_days)).isoformat()
            connection.execute(
                """INSERT INTO tombstone_snapshots(
                    snapshot_id, fact_id, content, summary, owner_principal,
                    domain, fact_type, visibility, sensitivity, egress_policy,
                    confidence_score, decay_tier, valid_from, valid_to,
                    recorded_at, updated_at, version_at_tombstone,
                    tombstoned_by, tombstoned_at, expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), cmd.fact_id, current["content"], current["summary"],
                 current["owner_principal"], current["domain"],
                 current["fact_type"], current["visibility"],
                 current["sensitivity"], current["egress_policy"],
                 current["confidence_score"], current["decay_tier"],
                 current["valid_from"], current["valid_to"],
                 current["recorded_at"], current["updated_at"],
                 int(current["current_version"]), actor_principal, now, _expires))
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
                actor_principal, source_event_id, created_at, content_hash,
                previous_version_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                fact_id,
                version,
                canonical_json(snapshot),
                change_type,
                change_reason,
                actor_principal,
                event_id,
                now,
                content_hash,
                _lineage_hash_for_new_version(connection, fact_id, version)),
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
                "SELECT owner_principal, status, domain, fact_type, sensitivity, visibility FROM facts WHERE fact_id=?", (fact_id,)
            ).fetchone()
            if not fact or fact["status"] == "tombstoned":
                return False
            from .auth import PRINCIPAL_ALIASES
            aliases = PRINCIPAL_ALIASES.get(principal_id, frozenset({principal_id}))
            owner_matches = (fact["owner_principal"] == principal_id) or (fact["owner_principal"] in aliases)

            # 1.5.0 共享放宽：仅对只读权限、非受限敏感度、且属于通用技术域的事实放行给已认证 Agent
            SHARED_DOMAINS = {"infrastructure", "knowledge", "tech_support"}
            SHARED_TYPES = {"reference", "learning"}
            domain_sharable = (
                permission == "read"
                and fact["domain"] in SHARED_DOMAINS
                and fact["fact_type"] in SHARED_TYPES
                and fact["sensitivity"] != "restricted"
                and "authenticated" in roles
            )
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
        return owner_default or domain_sharable or ("allow" in matched)

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

    # =========================================================================
    # v1.6.0: 实体知识库迁移
    # =========================================================================

    def migrate_schema_21_to_22(self) -> None:
        """从 SCHEMA_VERSION 21 迁移到 22：实体知识库基础表。

        该迁移只添加新表（entities、fact_entities、entity_relations），
        不修改或删除现有表/数据。schema_meta 中的 schema_version 会被
        更新为 "22"。
        """
        with self.transaction() as connection:
            # 更新元数据版本：实体表是运行时的加法扩展，版本号与当前运行时常量保持一致，
            # 这样迁移后的库在下次进程启动时仍能通过 _validate_existing_schema 校验。
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )

            # ---- 创建 entities 表 ----
            connection.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    description TEXT,
                    metadata_json TEXT,
                    sensitivity TEXT DEFAULT 'internal',
                    visibility TEXT DEFAULT 'all',
                    status TEXT DEFAULT 'active',
                    valid_from TEXT,
                    valid_to TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content_hash TEXT
                )
            """)

            # ---- 创建 fact_entities 表 ----
            connection.execute("""
                CREATE TABLE IF NOT EXISTS fact_entities (
                    fact_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    role TEXT,
                    confidence REAL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (fact_id, entity_id),
                    FOREIGN KEY (fact_id) REFERENCES facts(fact_id) ON DELETE CASCADE,
                    FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
                )
            """)

            # ---- 创建 entity_relations 表 ----
            connection.execute("""
                CREATE TABLE IF NOT EXISTS entity_relations (
                    from_entity_id TEXT NOT NULL,
                    to_entity_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    relation_direction TEXT DEFAULT 'forward',
                    strength REAL DEFAULT 1.0,
                    metadata_json TEXT,
                    valid_from TEXT,
                    valid_to TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (from_entity_id, to_entity_id, relation_type),
                    FOREIGN KEY (from_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
                    FOREIGN KEY (to_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
                )
            """)

            # ---- 创建索引 ----
            connection.execute("CREATE INDEX IF NOT EXISTS idx_entities_domain ON entities(domain)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_entities_valid ON entities(valid_from, valid_to)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_fact_entities_fact ON fact_entities(fact_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_fact_entities_role ON fact_entities(role)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_entity_relations_from ON entity_relations(from_entity_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_entity_relations_to ON entity_relations(to_entity_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_entity_relations_type ON entity_relations(relation_type)")

            # ---- 创建 probe_tasks 表（断供欠账重放队列）----
            connection.execute("""
                CREATE TABLE IF NOT EXISTS probe_tasks (
                    task_id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    due_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_probe_tasks_due ON probe_tasks(due_at, status)")

    # =========================================================================
    # v1.6.0: 实体 CRUD
    # =========================================================================

    def create_entity(
        self,
        entity_type: str,
        name: str,
        domain: str,
        *,
        description: str | None = None,
        metadata: dict | None = None,
        sensitivity: str = "internal",
        visibility: str = "all",
        status: str = "active",
        content_hash: str | None = None,
        now: str | None = None,
    ) -> dict:
        """创建结构化实体并返回生成的 entity_id。

        这是对 entities 表的封装写入，遵循同 fact_creation 相同的事务模式。
        """
        now = now or utc_now()
        entity_id = new_id()
        metadata_json = json.dumps(metadata) if metadata else None
        content_hash = content_hash or sha256_text(name)

        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO entities
                    (entity_id, entity_type, name, domain, description,
                     metadata_json, sensitivity, visibility, status,
                     valid_from, valid_to, created_at, updated_at, content_hash)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    entity_id,
                    entity_type,
                    name,
                    domain,
                    description,
                    metadata_json,
                    sensitivity,
                    visibility,
                    status,
                    now,  # created_at
                    now,  # updated_at
                    content_hash,
                ),
            )

        return {"entity_id": entity_id, "status": status}

    def get_entity(
        self, entity_id: str, *, now: str | None = None, include_suspended: bool = False
    ) -> dict | None:
        """根据 ID 获取实体（若已过期则返回 None）。

        双时态查询：仅在 valid_to 为 NULL 或 > now 时返回。

        默认排除 ``suspended`` 实体（与降级/断供标记隔离）；传入
        ``include_suspended=True`` 可读取被暂停的实体——下游判定"模型是否断供"
        正需要看到 suspended 状态，否则无法区分"已断供"与"不存在"。
        """
        now = now or utc_now()
        with contextlib.closing(self.connect()) as connection:
            if include_suspended:
                row = connection.execute(
                    """
                    SELECT entity_id, entity_type, name, domain, description,
                           metadata_json, sensitivity, visibility, status,
                           valid_from, valid_to, created_at, updated_at, content_hash
                    FROM entities
                    WHERE entity_id = ?
                      AND (valid_to IS NULL OR valid_to > ?)
                    """,
                    (entity_id, now),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT entity_id, entity_type, name, domain, description,
                           metadata_json, sensitivity, visibility, status,
                           valid_from, valid_to, created_at, updated_at, content_hash
                    FROM entities
                    WHERE entity_id = ?
                      AND status != 'suspended'
                      AND (valid_to IS NULL OR valid_to > ?)
                    """,
                    (entity_id, now),
                ).fetchone()
        if not row:
            return None
        return dict(row)

    def update_entity(
        self,
        entity_id: str,
        *,
        description: str | None = None,
        metadata: dict | None = None,
        sensitivity: str | None = None,
        visibility: str | None = None,
        status: str | None = None,
        valid_to: str | None = None,
        now: str | None = None,
    ) -> bool:
        """更新实体属性。仅传入需要更新的字段。

        返回 True 表示有行被更新，False 表示未找到/无变更。
        """
        now = now or utc_now()
        updates: list[str] = []
        params: list = []

        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if metadata is not None:
            updates.append("metadata_json = ?")
            params.append(json.dumps(metadata) if metadata else None)
        if sensitivity is not None:
            updates.append("sensitivity = ?")
            params.append(sensitivity)
        if visibility is not None:
            updates.append("visibility = ?")
            params.append(visibility)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if valid_to is not None:
            updates.append("valid_to = ?")
            params.append(valid_to)

        if not updates:
            return False

        updates.append("updated_at = ?")
        params.append(now)
        params.append(entity_id)

        with self.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE entities
                SET {', '.join(updates)}
                WHERE entity_id = ?
                """,
                params,
            )
        return cursor.rowcount > 0

    def soft_delete_entity(self, entity_id: str, *, now: str | None = None) -> bool:
        """软删除实体：将 status 设为 'deleted' 并记录 valid_to。

        这是推荐的删除方式，而非物理 DELETE，保留审计历史记录。
        使用 UTC now 确保时间一致性。
        """
        now = utc_now()
        return self.update_entity(
            entity_id,
            status="deleted",
            valid_to=now,
            now=now,
        )

    # =========================================================================
    # v1.6.0: 事实-实体关联
    # =========================================================================

    def link_fact_entity(
        self,
        fact_id: str,
        entity_id: str,
        *,
        role: str | None = None,
        confidence: float = 1.0,
        now: str | None = None,
    ) -> bool:
        """将事实与实体建立关联。

        如果相同 fact_id+entity_id 已存在，会 OR-Replace（相当于 UPSERT）。
        """
        now = now or utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO fact_entities (fact_id, entity_id, role, confidence, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(fact_id, entity_id)
                DO UPDATE SET role=excluded.role, confidence=excluded.confidence, created_at=excluded.created_at
                """,
                (fact_id, entity_id, role, confidence, now),
            )
        return True

    def unlink_fact_entity(self, fact_id: str, entity_id: str) -> bool:
        """移除事实与实体的关联。"""
        with self.transaction() as connection:
            cur = connection.execute(
                "DELETE FROM fact_entities WHERE fact_id=? AND entity_id=?",
                (fact_id, entity_id),
            )
            return cur.rowcount > 0

    def get_fact_entities(
        self,
        fact_id: str,
        *,
        now: str | None = None,
    ) -> list[dict]:
        """获取事实关联的所有实体（双时态筛选）。"""
        now = now or utc_now()
        with contextlib.closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT e.entity_id, e.entity_type, e.name, e.domain,
                       e.description, e.sensitivity, e.visibility, e.status,
                       e.valid_from, e.valid_to, e.created_at, e.updated_at,
                       e.content_hash,
                       fe.role, fe.confidence, fe.created_at AS linked_at
                FROM entities e
                JOIN fact_entities fe ON e.entity_id = fe.entity_id
                JOIN facts f ON fe.fact_id = f.fact_id
                WHERE fe.fact_id = ?
                  AND e.status != 'suspended'
                  AND (e.valid_to IS NULL OR e.valid_to > ?)
                  AND f.status = 'active'
                  AND f.valid_to IS NULL OR f.valid_to > ?
                ORDER BY fe.confidence DESC, e.name
                """,
                (fact_id, now, now),
            ).fetchall()
        return [
            {
                "entity_id": r[0],
                "entity_type": r[1],
                "name": r[2],
                "domain": r[3],
                "description": r[4],
                "sensitivity": r[5],
                "visibility": r[6],
                "status": r[7],
                "valid_from": r[8],
                "valid_to": r[9],
                "created_at": r[10],
                "updated_at": r[11],
                "content_hash": r[12],
                "role": r[13],
                "confidence": r[14],
                "linked_at": r[15],
            }
            for r in rows
        ]

    # =========================================================================
    # v1.6.0: 实体关系
    # =========================================================================

    def create_entity_relation(
        self,
        from_entity_id: str,
        to_entity_id: str,
        relation_type: str,
        *,
        relation_direction: str = "forward",
        strength: float = 1.0,
        metadata: dict | None = None,
        now: str | None = None,
    ) -> bool:
        """创建实体间的二元关系。

        如果相同 (from, to, relation_type) 已存在，会 OR-Replace。
        """
        now = now or utc_now()
        metadata_json = json.dumps(metadata) if metadata else None
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO entity_relations
                    (from_entity_id, to_entity_id, relation_type,
                     relation_direction, strength, metadata_json, created_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_entity_id, to_entity_id, relation_type)
                DO UPDATE SET
                    relation_direction=excluded.relation_direction,
                    strength=excluded.strength,
                    metadata_json=excluded.metadata_json,
                    created_at=excluded.created_at
                """,
                (
                    from_entity_id,
                    to_entity_id,
                    relation_type,
                    relation_direction,
                    strength,
                    metadata_json,
                    now,
                ),
            )
        return True

    def get_entity_relations(
        self,
        from_entity_id: str,
        *,
        relation_type: str | None = None,
        now: str | None = None,
    ) -> list[dict]:
        """获取指定实体的出边关系（双时态筛选）。"""
        now = now or utc_now()
        with contextlib.closing(self.connect()) as connection:
            if relation_type:
                rows = connection.execute(
                    """
                    SELECT e2.entity_id, e2.entity_type, e2.name, e2.domain,
                           e2.description, e2.sensitivity, e2.visibility, e2.status,
                           e2.valid_from, e2.valid_to, e2.created_at, e2.updated_at,
                           e2.content_hash,
                           er.relation_direction, er.strength, er.metadata_json, er.created_at AS related_at
                    FROM entities e1
                    JOIN entity_relations er ON e1.entity_id = er.from_entity_id
                    JOIN entities e2 ON er.to_entity_id = e2.entity_id
                    WHERE e1.entity_id = ?
                      AND e1.status != 'suspended'
                      AND e2.status != 'suspended'
                      AND (e2.valid_to IS NULL OR e2.valid_to > ?)
                      AND er.relation_type = ?
                      AND (er.valid_to IS NULL OR er.valid_to > ?)
                    ORDER BY er.strength DESC, e2.name
                    """,
                    (from_entity_id, now, relation_type, now),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT e2.entity_id, e2.entity_type, e2.name, e2.domain,
                           e2.description, e2.sensitivity, e2.visibility, e2.status,
                           e2.valid_from, e2.valid_to, e2.created_at, e2.updated_at,
                           e2.content_hash,
                           er.relation_direction, er.strength, er.metadata_json, er.created_at AS related_at
                    FROM entities e1
                    JOIN entity_relations er ON e1.entity_id = er.from_entity_id
                    JOIN entities e2 ON er.to_entity_id = e2.entity_id
                    WHERE e1.entity_id = ?
                      AND e1.status != 'suspended'
                      AND e2.status != 'suspended'
                      AND (e2.valid_to IS NULL OR e2.valid_to > ?)
                      AND (er.valid_to IS NULL OR er.valid_to > ?)
                    ORDER BY er.strength DESC, e2.name
                    """,
                    (from_entity_id, now, now),
                ).fetchall()

        return [
            {
                "from_entity_id": from_entity_id,
                "to_entity_id": r[0],
                "to_entity_type": r[1],
                "to_name": r[2],
                "to_domain": r[3],
                "to_description": r[4],
                "to_sensitivity": r[5],
                "to_visibility": r[6],
                "to_status": r[7],
                "to_valid_from": r[8],
                "to_valid_to": r[9],
                "to_created_at": r[10],
                "to_updated_at": r[11],
                "to_content_hash": r[12],
                "relation_direction": r[13],
                "strength": r[14],
                "metadata_json": r[15],
                "related_at": r[16],
            }
            for r in rows
        ]

    def get_incoming_relations(
        self,
        to_entity_id: str,
        *,
        relation_type: str | None = None,
        now: str | None = None,
    ) -> list[dict]:
        """获取指定实体的入边关系（谁指向了该实体）。

        通过反向查询 entity_relations 的 from_entity_id 来实现。
        """
        now = now or utc_now()
        with contextlib.closing(self.connect()) as connection:
            if relation_type:
                rows = connection.execute(
                    """
                    SELECT e1.entity_id, e1.entity_type, e1.name, e1.domain,
                           e1.description, e1.sensitivity, e1.visibility, e1.status,
                           e1.valid_from, e1.valid_to, e1.created_at, e1.updated_at,
                           e1.content_hash,
                           er.relation_direction, er.strength, er.metadata_json, er.created_at AS related_at
                    FROM entities e2
                    JOIN entity_relations er ON e2.entity_id = er.to_entity_id
                    JOIN entities e1 ON er.from_entity_id = e1.entity_id
                    WHERE e2.entity_id = ?
                      AND e2.status != 'suspended'
                      AND e1.status != 'suspended'
                      AND (e1.valid_to IS NULL OR e1.valid_to > ?)
                      AND er.relation_type = ?
                      AND (er.valid_to IS NULL OR er.valid_to > ?)
                    ORDER BY er.strength DESC, e1.name
                    """,
                    (to_entity_id, now, relation_type, now),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT e1.entity_id, e1.entity_type, e1.name, e1.domain,
                           e1.description, e1.sensitivity, e1.visibility, e1.status,
                           e1.valid_from, e1.valid_to, e1.created_at, e1.updated_at,
                           e1.content_hash,
                           er.relation_direction, er.strength, er.metadata_json, er.created_at AS related_at
                    FROM entities e2
                    JOIN entity_relations er ON e2.entity_id = er.to_entity_id
                    JOIN entities e1 ON er.from_entity_id = e1.entity_id
                    WHERE e2.entity_id = ?
                      AND e2.status != 'suspended'
                      AND e1.status != 'suspended'
                      AND (e1.valid_to IS NULL OR e1.valid_to > ?)
                      AND (er.valid_to IS NULL OR er.valid_to > ?)
                    ORDER BY er.strength DESC, e1.name
                    """,
                    (to_entity_id, now, now),
                ).fetchall()

        return [
            {
                "from_entity_id": r[0],
                "from_entity_type": r[1],
                "from_name": r[2],
                "from_domain": r[3],
                "from_description": r[4],
                "from_sensitivity": r[5],
                "from_visibility": r[6],
                "from_status": r[7],
                "from_valid_from": r[8],
                "from_valid_to": r[9],
                "from_created_at": r[10],
                "from_updated_at": r[11],
                "from_content_hash": r[12],
                "relation_direction": r[13],
                "strength": r[14],
                "metadata_json": r[15],
                "related_at": r[16],
            }
            for r in rows
        ]

    # =========================================================================
    # v1.6.0 Step 2: 模型探针调度 + 断供欠账重放
    # =========================================================================

    def probe_model_endpoint(
        self,
        endpoint: str,
        timeout: float = 30,
        *,
        _opener=None,
    ) -> dict:
        """对模型端点发送轻量 HTTP GET，返回探测结果。

        返回结构：
            {"status": "ok"|"unreachable", "latency_ms": float, "error": str|None}

        该方法保证在 ``timeout`` 内返回（超时即视为 unreachable），不会阻塞
        调用方。``_opener`` 仅供测试注入，生产环境使用标准 urllib 打开器。
        """
        opener = _opener or urllib.request.build_opener()
        start = datetime.now(timezone.utc)
        try:
            req = urllib.request.Request(endpoint, method="GET")
            with opener.open(req, timeout=timeout) as resp:
                resp.status  # 触发网络 IO，确认端点可达
            status = "ok"
            error = None
        except Exception as exc:  # noqa: BLE001 - 探针需把所有失败归一为 unreachable
            status = "unreachable"
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000.0
        return {"status": status, "latency_ms": round(elapsed_ms, 3), "error": error}

    def record_model_probe(
        self,
        model_name: str,
        domain: str,
        outcome: dict,
        *,
        fact_id: str,
        actor_principal: str,
        now: str | None = None,
    ) -> str:
        """将一次模型探针的结果持久化到 entities 表并关联到目标 fact。

        探测结果以 ``model_probe`` 类型的实体保存；探针为 ok 时实体状态为
        active，为 unreachable 时状态为 suspended（供下游判定断供）。同时把
        该实体以 ``probe_target`` 角色关联到传入的 fact。

        返回新创建/复用的实体 entity_id。
        """
        now = now or utc_now()
        status = "suspended" if outcome.get("status") == "unreachable" else "active"
        metadata = {
            "endpoint": outcome.get("endpoint"),
            "status": outcome.get("status"),
            "latency_ms": outcome.get("latency_ms"),
            "error": outcome.get("error"),
            "expected_status": outcome.get("expected_status"),
            "probed_at": now,
        }
        entity = self.create_entity(
            entity_type="model_probe",
            name=model_name,
            domain=domain,
            description=f"探针结果：{outcome.get('status')} @ {outcome.get('endpoint')}",
            metadata=metadata,
            sensitivity="internal",
            visibility="all",
            status=status,
            now=now,
        )
        self.link_fact_entity(
            fact_id, entity["entity_id"], role="probe_target", confidence=1.0, now=now
        )
        return entity["entity_id"]

    def queue_probe_task(
        self,
        entity_id: str,
        endpoint: str,
        snapshot: dict,
        *,
        due_at: str,
        now: str | None = None,
    ) -> str:
        """将一次失败的探针排入断供欠账重放队列。

        返回新创建的任务 task_id。``due_at`` 为该任务可被执行的最早时间
        （ISO 8601 字符串），``snapshot`` 为失败时的探针结果快照。
        """
        now = now or utc_now()
        task_id = new_id()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO probe_tasks
                    (task_id, entity_id, endpoint, snapshot_json, status, due_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    task_id,
                    entity_id,
                    endpoint,
                    json.dumps(snapshot, ensure_ascii=False),
                    due_at,
                    now,
                ),
            )
        return task_id

    def get_due_probe_tasks(self, *, now: str) -> list[dict]:
        """返回在 ``now`` 之前到期且仍未完成的探针重放任务。"""
        with contextlib.closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT task_id, entity_id, endpoint, snapshot_json, status, due_at, completed_at
                FROM probe_tasks
                WHERE status = 'pending' AND due_at <= ?
                ORDER BY due_at ASC
                """,
                (now,),
            ).fetchall()
        return [
            {
                "task_id": r[0],
                "entity_id": r[1],
                "endpoint": r[2],
                "snapshot": json.loads(r[3]) if r[3] else None,
                "status": r[4],
                "due_at": r[5],
                "completed_at": r[6],
            }
            for r in rows
        ]

    def complete_probe_task(self, task_id: str, result: dict, *, now: str | None = None) -> bool:
        """标记重放任务完成，并写入最终探针结果。

        返回是否有行被更新（任务存在且原本为 pending 时为 True）。
        """
        now = now or utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE probe_tasks
                SET status = 'completed', completed_at = ?, snapshot_json = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (now, json.dumps(result, ensure_ascii=False), task_id),
            )
            return cursor.rowcount > 0

