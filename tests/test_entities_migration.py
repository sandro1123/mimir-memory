"""Migration verification tests for Mímir v1.6.0 entity knowledge base.

Tests that the SCHEMA_VERSION 21 → 22 migration correctly creates
the entities, fact_entities, and entity_relations tables.
"""
import pytest
import tempfile
from pathlib import Path
from mimir_v8.store import CanonicalStore


def test_migration_preserves_schema_version_and_creates_entity_tables():
    """Entity migration is additive at the current runtime version.

    The entity tables are an additive extension of the existing schema, so the
    migration does NOT bump the schema_version number (that would require a
    matching framework migration segment + MIGRATABLE_TARGET_VERSIONS entry).
    After migration the version must still equal SCHEMA_VERSION so the DB
    passes validation on the next process start.
    """
    from mimir_v8.schema import SCHEMA_VERSION
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")

        # Plant a baseline at the current runtime version.
        conn = store.connect()
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
        before = cursor.fetchone()["value"]
        conn.close()

        # Run migration
        store.migrate_schema_21_to_22()

        # Version stays consistent with the runtime constant.
        conn = store.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
        after = cursor.fetchone()["value"]
        # Entity tables now exist.
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entities'"
        )
        has_entities = cursor.fetchone() is not None
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='probe_tasks'"
        )
        has_probe_tasks = cursor.fetchone() is not None
        conn.close()

        assert before == str(SCHEMA_VERSION)
        assert after == str(SCHEMA_VERSION), (
            f"entity migration must not change schema_version; got {after!r}"
        )
        assert has_entities, "entities table should exist after migration"
        assert has_probe_tasks, "probe_tasks table should exist after migration"


def test_migration_creates_entities_table():
    """The 'entities' table should exist after migration."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")

        store.migrate_schema_21_to_22()

        conn = store.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entities'")
        row = cursor.fetchone()
        assert row is not None, "entities table should exist after migration"
        conn.close()


def test_migration_creates_fact_entities_table():
    """The 'fact_entities' table should exist after migration."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")

        store.migrate_schema_21_to_22()

        conn = store.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fact_entities'")
        row = cursor.fetchone()
        assert row is not None, "fact_entities table should exist after migration"
        conn.close()


def test_migration_creates_entity_relations_table():
    """The 'entity_relations' table should exist after migration."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")

        store.migrate_schema_21_to_22()

        conn = store.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entity_relations'")
        row = cursor.fetchone()
        assert row is not None, "entity_relations table should exist after migration"
        conn.close()


def test_migration_table_structure():
    """Each new table should have the expected columns."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")

        store.migrate_schema_21_to_22()

        conn = store.connect()
        cursor = conn.cursor()

        # Check entities table columns
        cursor.execute("PRAGMA table_info(entities)")
        entities_cols = {row[1] for row in cursor.fetchall()}
        expected_entities_cols = {
            "entity_id", "entity_type", "name", "domain", "description",
            "metadata_json", "sensitivity", "visibility", "status",
            "valid_from", "valid_to", "created_at", "updated_at", "content_hash"
        }
        assert expected_entities_cols.issubset(entities_cols), (
            f"entities table missing columns. Have: {entities_cols}, "
            f"Expected subset: {expected_entities_cols}"
        )

        # Check fact_entities table columns
        cursor.execute("PRAGMA table_info(fact_entities)")
        fact_entities_cols = {row[1] for row in cursor.fetchall()}
        expected_fact_entities_cols = {"fact_id", "entity_id", "role", "confidence", "created_at"}
        assert expected_fact_entities_cols.issubset(fact_entities_cols), (
            f"fact_entities table missing columns. Have: {fact_entities_cols}, "
            f"Expected subset: {expected_fact_entities_cols}"
        )

        # Check entity_relations table columns
        cursor.execute("PRAGMA table_info(entity_relations)")
        entity_rels_cols = {row[1] for row in cursor.fetchall()}
        expected_entity_rels_cols = {
            "from_entity_id", "to_entity_id", "relation_type",
            "relation_direction", "strength", "metadata_json", "created_at"
        }
        assert expected_entity_rels_cols.issubset(entity_rels_cols), (
            f"entity_relations table missing columns. Have: {entity_rels_cols}, "
            f"Expected subset: {expected_entity_rels_cols}"
        )

        conn.close()