"""Idempotent v7 snapshot importer for Mímir v8."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .classifier import SOURCE_CATEGORY_MAP
from .conflict import V16_ADDITIVE_STATEMENTS
from .crystallize import V17_ADDITIVE_STATEMENTS
from .multimodal import V18_ADDITIVE_STATEMENTS
from .schema import CreateFact, SCHEMA_VERSION
from .symbolic_memory import V14_ADDITIVE_STATEMENTS
from .evolve import V15_ADDITIVE_STATEMENTS
from .store import CanonicalStore, canonical_json, new_id, sha256_text, utc_now


V11_ADDITIVE_STATEMENTS = (
    """CREATE TABLE knowledge_items (
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
    ) STRICT""",
    """CREATE TABLE knowledge_feedback_signals (
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
    ) STRICT""",
    """CREATE TABLE governance_suggestions (
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
    ) STRICT""",
    "CREATE INDEX idx_knowledge_items_search ON knowledge_items(layer, status, domain, owner_principal, updated_at)",
    "CREATE INDEX idx_knowledge_items_source ON knowledge_items(source_category, source_hash, updated_at)",
    "CREATE INDEX idx_knowledge_feedback_target ON knowledge_feedback_signals(target_layer, target_id, created_at)",
    "CREATE INDEX idx_governance_suggestions_status ON governance_suggestions(status, target_layer, created_at)",
)


# ── v10 (schema 12 → 13) additive: opinion & observation tables ──────────
V13_ADDITIVE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS opinions (
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
    ) STRICT""",
    """CREATE TABLE IF NOT EXISTS observations (
        observation_id TEXT PRIMARY KEY,
        summary TEXT NOT NULL,
        supporting_opinion_ids TEXT NOT NULL DEFAULT '[]',
        confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
        stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0,1)),
        owner_principal TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT""",
    "CREATE INDEX IF NOT EXISTS idx_opinions_topic ON opinions(topic, owner_principal)",
    "CREATE INDEX IF NOT EXISTS idx_opinions_fact ON opinions(fact_id)",
    "CREATE INDEX IF NOT EXISTS idx_observations_owner ON observations(owner_principal, confidence DESC)",
)


class MigrationError(RuntimeError):
    """Raised when data or schema cannot be migrated without losing meaning."""


@dataclass(frozen=True)
class SchemaMigrationReport:
    source_version: int
    target_version: int
    database: str
    backup: str
    backup_sha256: str
    migrated_rows: int

    def as_dict(self) -> dict:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "database": self.database,
            "backup": self.backup,
            "backup_sha256": self.backup_sha256,
            "migrated_rows": self.migrated_rows,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_schema_version(connection: sqlite3.Connection) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if not table:
        raise MigrationError("database has no schema_meta table")
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        raise MigrationError("database has no schema_version")
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"invalid schema_version: {row[0]!r}") from exc


def _online_backup(source: Path, destination: Path) -> str:
    if source.resolve() == destination.resolve():
        raise MigrationError("migration backup must differ from the database")
    if destination.exists():
        raise MigrationError("migration backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with contextlib.closing(sqlite3.connect(source, timeout=30.0)) as source_db:
            source_db.execute("PRAGMA busy_timeout=30000")
            with contextlib.closing(sqlite3.connect(temporary)) as backup_db:
                source_db.backup(backup_db)
                backup_db.commit()
        with contextlib.closing(sqlite3.connect(temporary)) as verification:
            if verification.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationError("migration backup failed quick_check")
        temporary.replace(destination)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    return _sha256_file(destination)


def migrate_schema_v13(
    database: str | Path,
    backup: str | Path,
) -> SchemaMigrationReport:
    """Migrate schema 12 -> 13: add opinions + observations tables."""
    database_path = Path(database)
    backup_path = Path(backup)
    if not database_path.is_file():
        raise MigrationError("migration database does not exist")
    with contextlib.closing(sqlite3.connect(database_path)) as probe:
        source_version = _read_schema_version(probe)
    if source_version == SCHEMA_VERSION:
        raise MigrationError("database is already at the runtime schema")
    if source_version != 12:
        raise MigrationError(f"schema 12 backup required for v13 migration, found: {source_version}")

    backup_sha256 = _online_backup(database_path, backup_path)

    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")

        for statement in V13_ADDITIVE_STATEMENTS:
            connection.execute(statement)

        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("foreign-key violations detected during migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    CanonicalStore(database_path)
    return SchemaMigrationReport(
        source_version=source_version,
        target_version=SCHEMA_VERSION,
        database=str(database_path),
        backup=str(backup_path),
        backup_sha256=backup_sha256,
        migrated_rows=0,
    )


def migrate_schema_v14(
    database: str | Path,
    backup: str | Path,
) -> SchemaMigrationReport:
    """Migrate schema 13 -> 14: symbolic short-term memory + code graph tables."""
    database_path = Path(database)
    backup_path = Path(backup)
    if not database_path.is_file():
        raise MigrationError("migration database does not exist")
    with contextlib.closing(sqlite3.connect(database_path)) as probe:
        source_version = _read_schema_version(probe)
    if source_version == SCHEMA_VERSION:
        raise MigrationError("database is already at the runtime schema")
    if source_version != 13:
        raise MigrationError(f"schema 13 backup required for v14 migration, found: {source_version}")

    backup_sha256 = _online_backup(database_path, backup_path)

    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")

        for statement in V14_ADDITIVE_STATEMENTS:
            connection.execute(statement)

        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("foreign-key violations detected during migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    CanonicalStore(database_path)
    return SchemaMigrationReport(
        source_version=source_version,
        target_version=SCHEMA_VERSION,
        database=str(database_path),
        backup=str(backup_path),
        backup_sha256=backup_sha256,
        migrated_rows=0,
    )


def migrate_schema_v15(
    database: str | Path,
    backup: str | Path,
) -> SchemaMigrationReport:
    """Migrate schema 14 -> 15: EvolveMem tables (search_feedback, quality_metrics)."""
    database_path = Path(database)
    backup_path = Path(backup)
    if not database_path.is_file():
        raise MigrationError("migration database does not exist")
    with contextlib.closing(sqlite3.connect(database_path)) as probe:
        source_version = _read_schema_version(probe)
    if source_version == SCHEMA_VERSION:
        raise MigrationError("database is already at the runtime schema")
    if source_version != 14:
        raise MigrationError(f"schema 14 backup required for v15 migration, found: {source_version}")

    backup_sha256 = _online_backup(database_path, backup_path)

    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")

        for statement in V15_ADDITIVE_STATEMENTS:
            connection.execute(statement)

        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("foreign-key violations detected during migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    CanonicalStore(database_path)
    return SchemaMigrationReport(
        source_version=source_version,
        target_version=SCHEMA_VERSION,
        database=str(database_path),
        backup=str(backup_path),
        backup_sha256=backup_sha256,
        migrated_rows=0,
    )


def migrate_schema_v19(
    database: str | Path,
    backup: str | Path,
) -> SchemaMigrationReport:
    """Migrate schema 18 -> 19: vault-aware conversation_sources CHECK.

    Databases created at v8 froze a connector_type CHECK whitelist
    before the vault collector existed, so the first real vault harvest
    (2026-09-02) failed with IntegrityError on every note. SQLite
    cannot ALTER a CHECK constraint, so the table is rebuilt: new table
    with the vault-aware whitelist -> copy rows -> swap names. Row
    count and content are verified after the swap.
    """
    database_path = Path(database)
    backup_path = Path(backup)
    if not database_path.is_file():
        raise MigrationError("migration database does not exist")
    with contextlib.closing(sqlite3.connect(database_path)) as probe:
        source_version = _read_schema_version(probe)
    if source_version == SCHEMA_VERSION:
        raise MigrationError("database is already at the runtime schema")
    if source_version != 18:
        raise MigrationError(f"schema 18 backup required for v19 migration, found: {source_version}")

    backup_sha256 = _online_backup(database_path, backup_path)

    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        rows = _rebuild_conversation_sources_v19(connection)
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("foreign-key violations detected during migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.close()

    CanonicalStore(database_path)
    return SchemaMigrationReport(
        source_version=source_version,
        target_version=SCHEMA_VERSION,
        database=str(database_path),
        backup=str(backup_path),
        backup_sha256=backup_sha256,
        migrated_rows=rows,
    )


def _rebuild_conversation_sources_v19(connection):
    """Rebuild conversation_sources with the vault-aware CHECK whitelist.

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt:
    new table -> copy rows -> swap names. Must run inside a caller-owned
    transaction. Conversation tables reference this one, so callers wrap
    this with PRAGMA foreign_keys=OFF and verify with PRAGMA
    foreign_key_check afterwards (classic 12-step ALTER recipe).
    Raises MigrationError if the row count changes across the swap.
    """
    before = connection.execute(
        "SELECT COUNT(*) FROM conversation_sources"
    ).fetchone()[0]
    connection.execute(
        """
        CREATE TABLE conversation_sources_v19 (
            source_id TEXT PRIMARY KEY,
            connector_type TEXT NOT NULL,
            connector_id TEXT NOT NULL,
            session_id TEXT,
            source_uri TEXT,
            source_hash TEXT NOT NULL,
            title TEXT,
            owner_principal TEXT NOT NULL,
            retention_class TEXT NOT NULL CHECK (retention_class IN
                ('session','short','standard','permanent','legal_hold')),
            memory_mode TEXT NOT NULL CHECK (memory_mode IN ('explicit','observe','never')),
            source_category TEXT NOT NULL CHECK (source_category IN
                ('conversation','external_info','knowledge_doc','unknown/quarantine')),
            started_at TEXT,
            ended_at TEXT,
            ingested_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        ) STRICT
        """
    )
    connection.execute(
        """
        INSERT INTO conversation_sources_v19
            (source_id, connector_type, connector_id, session_id, source_uri,
             source_hash, title, owner_principal, retention_class, memory_mode,
             source_category, started_at, ended_at, ingested_at, metadata_json)
        SELECT source_id, connector_type, connector_id, session_id, source_uri,
               source_hash, title, owner_principal, retention_class, memory_mode,
               source_category, started_at, ended_at, ingested_at, metadata_json
        FROM conversation_sources
        """
    )
    connection.execute("DROP TABLE conversation_sources")
    connection.execute(
        "ALTER TABLE conversation_sources_v19 RENAME TO conversation_sources"
    )
    after = connection.execute(
        "SELECT COUNT(*) FROM conversation_sources"
    ).fetchone()[0]
    if after != before:
        raise MigrationError(
            f"conversation_sources row count changed during rebuild: {before} -> {after}"
        )
    return after


def _additive_chain(source_version: int) -> tuple:
    """Return the additive statement groups needed to reach the runtime schema."""
    chain = []
    if source_version < 11:
        chain.append(V11_ADDITIVE_STATEMENTS)
    if source_version < 13:
        chain.append(V13_ADDITIVE_STATEMENTS)
    if source_version < 14:
        chain.append(V14_ADDITIVE_STATEMENTS)
    if source_version < 15:
        chain.append(V15_ADDITIVE_STATEMENTS)
    if source_version < 16:
        chain.append(V16_ADDITIVE_STATEMENTS)
    if source_version < 17:
        chain.append(V17_ADDITIVE_STATEMENTS)
    if source_version < 18:
        chain.append(V18_ADDITIVE_STATEMENTS)
    return tuple(chain)


def migrate_schema(
    database: str | Path,
    backup: str | Path,
    *,
    failure_hook: Callable[[str], None] | None = None,
) -> SchemaMigrationReport:
    """Explicitly migrate a canonical database to the runtime schema.

    Schema 9 and 10 are supported inputs. A verified online backup is mandatory.
    Source-category backfill and the additive v11 knowledge tables, plus the
    v13/v14/v15/v16 additive tables, are committed in one transaction; partially
    applied v11 structures are rejected.
    """
    database_path = Path(database)
    backup_path = Path(backup)
    if not database_path.is_file():
        raise MigrationError("migration database does not exist")
    with contextlib.closing(sqlite3.connect(database_path)) as probe:
        source_version = _read_schema_version(probe)
    if source_version == SCHEMA_VERSION:
        raise MigrationError("database is already at the runtime schema")
    if source_version not in {9, 10, 11, 12, 13, 14, 15, 16, 17, 18} or SCHEMA_VERSION not in {11, 14, 15, 16, 17, 18, 19}:
        raise MigrationError(
            f"unsupported schema migration: {source_version} -> {SCHEMA_VERSION}"
        )
    with contextlib.closing(sqlite3.connect(database_path)) as probe:
        existing_v11 = {
            row[0] for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('knowledge_items','knowledge_feedback_signals','governance_suggestions')"
            )
        }
    if source_version in (9, 10) and existing_v11:
        raise MigrationError(
            f"database contains partial v11 structures at schema {source_version}: {sorted(existing_v11)}"
        )
    backup_sha256 = _online_backup(database_path, backup_path)
    if failure_hook:
        failure_hook("after_backup")

    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        migrated_rows = 0
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversation_sources)")
        }
        if source_version == 9:
            if "source_category" in columns:
                raise MigrationError("schema 9 unexpectedly contains source_category")
            connection.execute(
                "ALTER TABLE conversation_sources ADD COLUMN source_category TEXT NOT NULL "
                "DEFAULT 'unknown/quarantine' CHECK (source_category IN "
                "('conversation','external_info','knowledge_doc','unknown/quarantine'))"
            )
            rows = connection.execute(
                "SELECT source_id,connector_type FROM conversation_sources"
            ).fetchall()
            for source_id, connector_type in rows:
                category = SOURCE_CATEGORY_MAP.get(
                    connector_type.strip() if isinstance(connector_type, str) else "",
                    "unknown/quarantine",
                )
                migrated_rows += connection.execute(
                    "UPDATE conversation_sources SET source_category=? WHERE source_id=?",
                    (category, source_id),
                ).rowcount
        elif "source_category" not in columns:
            raise MigrationError("schema 10 is missing source_category")
        if failure_hook:
            failure_hook("after_source_category")
            failure_hook("after_backfill")
        for statements in _additive_chain(source_version):
            for statement in statements:
                connection.execute(statement)
        if failure_hook:
            failure_hook("after_v11_schema")
        if source_version <= 18 and SCHEMA_VERSION == 19:
            # v19: rebuild conversation_sources with the vault-aware
            # connector CHECK. Older databases froze the pre-vault
            # whitelist at creation time; the additive chain cannot
            # change a CHECK, so the rebuild runs here, inside the same
            # transaction, before the version stamp.
            connection.execute("PRAGMA foreign_keys=OFF")
            migrated_rows = _rebuild_conversation_sources_v19(connection)
            connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("foreign-key violations detected during migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    CanonicalStore(database_path)
    return SchemaMigrationReport(
        source_version=source_version,
        target_version=SCHEMA_VERSION,
        database=str(database_path),
        backup=str(backup_path),
        backup_sha256=backup_sha256,
        migrated_rows=migrated_rows,
    )


def restore_schema_backup(backup: str | Path, destination: str | Path) -> dict:
    """Restore a migration backup to a new isolated path and verify it."""
    backup_path = Path(backup)
    destination_path = Path(destination)
    if not backup_path.is_file():
        raise MigrationError("schema backup does not exist")
    if destination_path.exists():
        raise MigrationError("restore destination already exists")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(f".{destination_path.name}.tmp")
    try:
        shutil.copy2(backup_path, temporary)
        with contextlib.closing(sqlite3.connect(temporary)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationError("restored backup failed quick_check")
            version = _read_schema_version(connection)
        temporary.replace(destination_path)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    return {
        "backup": str(backup_path),
        "destination": str(destination_path),
        "sha256": _sha256_file(destination_path),
        "schema_version": version,
    }


def normalize_tags(value: object) -> list[str]:
    current = value
    for _ in range(4):
        if not isinstance(current, str):
            break
        stripped = current.strip()
        if not stripped:
            return []
        try:
            current = json.loads(stripped)
            continue
        except (TypeError, ValueError):
            current = [item.strip() for item in stripped.split(",")]
            break
    if not isinstance(current, list):
        current = [current]
    result = []
    seen = set()
    for item in current:
        cleaned = str(item).strip().strip("[]").strip().strip("\"'").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned[:80])
    return result


@dataclass(frozen=True)
class MigrationReport:
    scanned: int
    created: int
    unchanged: int
    relations_created: int
    warnings: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "created": self.created,
            "unchanged": self.unchanged,
            "relations_created": self.relations_created,
            "warnings": list(self.warnings),
        }


class V7Importer:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def import_records(self, records: list[dict], actor_principal: str = "admin") -> MigrationReport:
        created = 0
        unchanged = 0
        warnings: list[str] = []
        migrated: dict[str, str] = {}
        pending_relations: list[tuple[str, str]] = []

        for index, record in enumerate(records):
            legacy_id = str(record.get("fact_id") or "").strip()
            content = record.get("content")
            metadata = record.get("metadata") or {}
            if not legacy_id or not isinstance(content, str) or not content.strip():
                raise MigrationError(f"record {index} lacks fact_id or content")
            source_hash = sha256_text(
                canonical_json(
                    {"fact_id": legacy_id, "content": content, "metadata": metadata}
                )
            )
            supersedes = str(metadata.get("supersedes") or "").strip()
            if supersedes:
                for old_id in (item.strip() for item in supersedes.split(",")):
                    if old_id:
                        pending_relations.append((legacy_id, old_id))
            with contextlib.closing(self.store.connect()) as connection:
                existing = connection.execute(
                    """SELECT f.fact_id, f.content_hash, s.content_hash AS source_hash
                    FROM facts f
                    LEFT JOIN fact_sources fs ON fs.fact_id=f.fact_id AND fs.version=1
                    LEFT JOIN sources s ON s.source_id=fs.source_id
                    WHERE f.legacy_id=?""",
                    (legacy_id,),
                ).fetchone()
            if existing:
                if existing["content_hash"] != sha256_text(content.strip()):
                    raise MigrationError(
                        f"legacy_id {legacy_id} already exists with different content"
                    )
                if existing["source_hash"] and existing["source_hash"] != source_hash:
                    raise MigrationError(
                        f"legacy_id {legacy_id} already exists with different metadata"
                    )
                migrated[legacy_id] = existing["fact_id"]
                unchanged += 1
                continue

            visibility = metadata.get("visibility", "all")
            tags = normalize_tags(metadata.get("tags"))
            source_uri = f"mimir-v7://fact/{legacy_id}"
            summary = metadata.get("summary") or content[:200]
            result = self.store.create_fact(
                CreateFact(
                    content=content,
                    summary=summary,
                    owner_principal=metadata.get("agent_id", "heimdallr"),
                    domain=metadata.get("domain", "system"),
                    fact_type=metadata.get("fact_type", "reference"),
                    visibility=visibility,
                    sensitivity="internal",
                    egress_policy="local_only",
                    human_status="unreviewed",
                    valid_from=metadata.get("valid_from") or None,
                    valid_to=metadata.get("valid_to") or None,
                    recorded_at=metadata.get("created_at") or None,
                    legacy_id=legacy_id,
                    event_type="fact.migrated",
                    source_kind="mimir_v7_snapshot",
                    source_uri=source_uri,
                    source_hash=source_hash,
                    idempotency_key=f"migrate-v7:{legacy_id}:{source_hash}",
                ),
                actor_principal=actor_principal,
            )
            migrated[legacy_id] = result["fact_id"]
            created += 1
            with self.store.transaction() as connection:
                for tag in tags:
                    relation_id = new_id()
                    connection.execute(
                        """INSERT INTO relations(
                            relation_id, source_fact_id, target_type, target_id,
                            relation_type, status, created_by, created_at, source_event_id
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            relation_id,
                            result["fact_id"],
                            "tag",
                            tag,
                            "about_entity",
                            "active",
                            actor_principal,
                            utc_now(),
                            result["event_id"],
                        ),
                    )

        relations_created = 0
        for new_legacy_id, old_legacy_id in pending_relations:
            source_fact_id = migrated.get(new_legacy_id)
            target_fact_id = migrated.get(old_legacy_id)
            if not target_fact_id:
                with contextlib.closing(self.store.connect()) as connection:
                    row = connection.execute(
                        "SELECT fact_id FROM facts WHERE legacy_id=?", (old_legacy_id,)
                    ).fetchone()
                target_fact_id = row["fact_id"] if row else None
            if not source_fact_id or not target_fact_id:
                warnings.append(
                    f"unresolved supersedes relation: {new_legacy_id} -> {old_legacy_id}"
                )
                continue
            with self.store.transaction() as connection:
                exists = connection.execute(
                    """SELECT 1 FROM relations WHERE source_fact_id=? AND target_type='fact'
                    AND target_id=? AND relation_type='supersedes'""",
                    (source_fact_id, target_fact_id),
                ).fetchone()
                if exists:
                    continue
                event_id = connection.execute(
                    "SELECT source_event_id FROM fact_versions WHERE fact_id=? AND version=1",
                    (source_fact_id,),
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO relations(
                        relation_id, source_fact_id, target_type, target_id,
                        relation_type, status, created_by, created_at, source_event_id
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id(),
                        source_fact_id,
                        "fact",
                        target_fact_id,
                        "supersedes",
                        "active",
                        actor_principal,
                        utc_now(),
                        event_id,
                    ),
                )
                relations_created += 1

        return MigrationReport(
            scanned=len(records),
            created=created,
            unchanged=unchanged,
            relations_created=relations_created,
            warnings=tuple(warnings),
        )

    def import_json(self, path: str | Path, actor_principal: str = "admin") -> MigrationReport:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        records = data.get("records") if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise MigrationError("snapshot must be a list or an object containing records")
        return self.import_records(records, actor_principal=actor_principal)
