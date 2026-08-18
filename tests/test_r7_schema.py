from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mimir_v8.migration import MigrationError, migrate_schema, restore_schema_backup
from mimir_v8.schema import SCHEMA_VERSION
from mimir_v8.store import CanonicalStore, SchemaVersionError


V11_TABLES = {"knowledge_items", "knowledge_feedback_signals", "governance_suggestions"}


class TestR7SchemaLifecycle(unittest.TestCase):
    @staticmethod
    def _tables(path: Path) -> set[str]:
        with contextlib.closing(sqlite3.connect(path)) as connection:
            return {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}

    @staticmethod
    def _version(path: Path) -> int:
        with contextlib.closing(sqlite3.connect(path)) as connection:
            return int(connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0])

    @staticmethod
    def _make_v10(root: Path) -> Path:
        database = root / "v10.db"
        store = CanonicalStore(root / "fresh-v11.db")
        with contextlib.closing(store.connect()) as source, contextlib.closing(sqlite3.connect(database)) as target:
            source.backup(target)
            target.execute("PRAGMA foreign_keys=OFF")
            for table in V11_TABLES:
                target.execute(f"DROP TABLE {table}")
            target.execute("UPDATE schema_meta SET value='10' WHERE key='schema_version'")
            target.commit()
        return database

    @staticmethod
    def _make_v9(root: Path) -> Path:
        database = TestR7SchemaLifecycle._make_v10(root)
        rebuilt = root / "v9.db"
        with contextlib.closing(sqlite3.connect(database)) as source, contextlib.closing(sqlite3.connect(rebuilt)) as target:
            source.backup(target)
        with contextlib.closing(sqlite3.connect(rebuilt)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("ALTER TABLE conversation_sources RENAME TO conversation_sources_v10")
            connection.execute("""CREATE TABLE conversation_sources (
                source_id TEXT PRIMARY KEY, connector_type TEXT NOT NULL, connector_id TEXT NOT NULL,
                session_id TEXT, source_uri TEXT, source_hash TEXT NOT NULL, title TEXT,
                owner_principal TEXT NOT NULL, retention_class TEXT NOT NULL,
                memory_mode TEXT NOT NULL, started_at TEXT, ended_at TEXT,
                ingested_at TEXT NOT NULL, metadata_json TEXT NOT NULL
            ) STRICT""")
            connection.execute("""INSERT INTO conversation_sources(
                source_id,connector_type,connector_id,session_id,source_uri,source_hash,title,
                owner_principal,retention_class,memory_mode,started_at,ended_at,ingested_at,metadata_json
            ) SELECT source_id,connector_type,connector_id,session_id,source_uri,source_hash,title,
                owner_principal,retention_class,memory_mode,started_at,ended_at,ingested_at,metadata_json
                FROM conversation_sources_v10""")
            connection.execute("DROP TABLE conversation_sources_v10")
            connection.execute("UPDATE schema_meta SET value='9' WHERE key='schema_version'")
            connection.commit()
        return rebuilt

    def test_fresh_database_contains_complete_v11_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "canonical.db"
            CanonicalStore(database)
            self.assertEqual(self._version(database), SCHEMA_VERSION)
            self.assertTrue(V11_TABLES.issubset(self._tables(database)))

    def test_fresh_and_migrated_v11_use_owner_scoped_uniqueness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = root / "fresh.db"
            CanonicalStore(fresh)
            migrated = self._make_v10(root)
            migrate_schema(migrated, root / "v10-backup.db")
            for database in (fresh, migrated):
                with contextlib.closing(sqlite3.connect(database)) as connection:
                    sql = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_items'"
                    ).fetchone()[0]
                normalized = "".join(sql.lower().split())
                self.assertIn(
                    "unique(layer,owner_principal,source_hash,content_hash)",
                    normalized,
                )

    def test_migrate_v10_to_v11_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_v10(root)
            backup = root / "v10-backup.db"
            report = migrate_schema(database, backup)
            self.assertEqual((report.source_version, report.target_version), (10, SCHEMA_VERSION))
            self.assertEqual(report.migrated_rows, 0)
            self.assertTrue(V11_TABLES.issubset(self._tables(database)))
            CanonicalStore(database)
            restored = root / "restored-v10.db"
            result = restore_schema_backup(backup, restored)
            self.assertEqual(result["schema_version"], 10)
            self.assertFalse(V11_TABLES & self._tables(restored))

    def test_migrate_v9_to_v11(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_v9(root)
            report = migrate_schema(database, root / "v9-backup.db")
            self.assertEqual((report.source_version, report.target_version), (9, SCHEMA_VERSION))
            with contextlib.closing(sqlite3.connect(database)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(conversation_sources)")}
            self.assertIn("source_category", columns)
            CanonicalStore(database)

    def test_failure_after_v11_schema_rolls_back_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_v10(root)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                migrate_schema(
                    database,
                    root / "backup.db",
                    failure_hook=lambda point: (_ for _ in ()).throw(RuntimeError("injected"))
                    if point == "after_v11_schema" else None,
                )
            self.assertEqual(self._version(database), 10)
            self.assertFalse(V11_TABLES & self._tables(database))
            with self.assertRaises(SchemaVersionError):
                CanonicalStore(database)

    def test_partial_v11_structure_is_rejected_before_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_v10(root)
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE knowledge_items(item_id TEXT PRIMARY KEY) STRICT")
                connection.commit()
            backup = root / "must-not-exist.db"
            with self.assertRaisesRegex(MigrationError, "partial v11"):
                migrate_schema(database, backup)
            self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
