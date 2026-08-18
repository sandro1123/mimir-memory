"""R6 production-preflight hardening tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mimir_v8.evaluator import Evaluator
from mimir_v8.migrate_cli import main as migrate_cli_main
from mimir_v8.migration import migrate_schema, restore_schema_backup
from mimir_v8.review import ReviewQueue
from mimir_v8.schema import SCHEMA_VERSION
from mimir_v8.store import CanonicalStore, SchemaVersionError, new_id, utc_now
from mimir_v8.worker import main as worker_main, review_reminder


class TestReviewReminderHardening(unittest.TestCase):
    def _insert_candidate(self, store: CanonicalStore, *, uncertainty: str, created_at: str) -> str:
        candidate_id = new_id()
        with store.transaction() as connection:
            connection.execute(
                """INSERT INTO candidate_facts(
                    candidate_id,status,content,summary,proposed_owner_principal,
                    proposed_domain,proposed_fact_type,proposed_visibility,
                    proposed_sensitivity,proposed_egress_policy,source_id,source_hash,
                    confidence_score,uncertainty_json,proposed_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate_id, "review_required", "test content", "test summary", "mentor",
                    "knowledge", "reference", "owner_only", "internal", "local_only",
                    None, None, 0.5, uncertainty, "service:test", created_at, utc_now(),
                ),
            )
        return candidate_id

    def test_real_schema_handles_mixed_historical_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            cases = (
                ('["automatic_extraction_requires_review"]', utc_now()),
                ('{"uncertainty_reasons":["secret detected"],"salience":0.8}', utc_now()),
                ('{invalid json}', utc_now()),
                ('42', utc_now()),
                ('{"salience":NaN}', utc_now()),
                ('[]', "not-a-timestamp"),
            )
            for uncertainty, created_at in cases:
                self._insert_candidate(store, uncertainty=uncertainty, created_at=created_at)
            result = review_reminder(store)
            self.assertEqual(result["total"], len(cases))
            self.assertGreaterEqual(result["parse_error_count"], 3)
            summary = ReviewQueue(store).summarize()
            self.assertEqual(summary.total, len(cases))
            self.assertGreaterEqual(len(summary.parse_errors), 3)
            self.assertIn("critical", summary.by_risk)

    def test_risk_filter_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            self._insert_candidate(
                store,
                uncertainty='{"uncertainty_reasons":["secret detected"]}',
                created_at=utc_now(),
            )
            self._insert_candidate(store, uncertainty="[]", created_at=utc_now())
            critical = ReviewQueue(store).list_pending(risk="critical")
            self.assertEqual(len(critical), 1)
            self.assertEqual(critical[0].risk, "critical")
            with self.assertRaises(ValueError):
                ReviewQueue(store).list_pending(risk="unknown")

    def test_non_finite_and_bool_salience_fall_back(self):
        self.assertEqual(ReviewQueue._infer_salience({"salience": float("nan")}), 0.5)
        self.assertEqual(ReviewQueue._infer_salience({"salience": True}), 0.5)


class TestEvaluatorR6Strictness(unittest.TestCase):
    def setUp(self):
        self.evaluator = Evaluator(api_key="test-key")

    @staticmethod
    def _valid_prefix() -> str:
        return (
            '"is_valuable":true,"salience":0.8,"risk":"low",'
            '"domain":"personal","fact_type":"user_pref",'
            '"summary":"s","reasoning":"r"'
        )

    def test_duplicate_json_key_is_rejected(self):
        raw = "{" + self._valid_prefix() + ',"risk":"critical"}'
        result = self.evaluator._parse_response(raw, "original content")
        self.assertIn("duplicate JSON key", result.parse_error)
        self.assertEqual(result.content, "original content")

    def test_parse_failure_preserves_content(self):
        raw = "{" + self._valid_prefix().replace('"summary":"s",', "") + "}"
        result = self.evaluator._parse_response(raw, "audit me")
        self.assertTrue(result.parse_error)
        self.assertEqual(result.content, "audit me")


class TestExplicitSchemaMigration(unittest.TestCase):
    def _make_legacy_v9(self, root: Path) -> Path:
        database = root / "canonical-v9.db"
        CanonicalStore(database)
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """INSERT INTO conversation_sources(
                    source_id,connector_type,connector_id,source_hash,title,
                    owner_principal,retention_class,memory_mode,source_category,
                    ingested_at,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "source-conversation", "hermes_cdc", "legacy", "h1", "conversation",
                    "mentor", "standard", "observe", "conversation", utc_now(), "{}",
                ),
            )
            connection.execute(
                """INSERT INTO conversation_sources(
                    source_id,connector_type,connector_id,source_hash,title,
                    owner_principal,retention_class,memory_mode,source_category,
                    ingested_at,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "source-rss", "rss", "legacy", "h2", "rss", "mentor", "standard",
                    "observe", "external_info", utc_now(), "{}",
                ),
            )
            for table in (
                "governance_suggestions",
                "knowledge_feedback_signals",
                "knowledge_items",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("ALTER TABLE conversation_sources DROP COLUMN source_category")
            connection.execute(
                "UPDATE schema_meta SET value='9' WHERE key='schema_version'"
            )
            connection.commit()
        return database

    def test_existing_legacy_database_is_not_silently_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self._make_legacy_v9(Path(tmp))
            with self.assertRaises(SchemaVersionError):
                CanonicalStore(database)
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
                columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(conversation_sources)"
                )}
            self.assertEqual(version, "9")
            self.assertNotIn("source_category", columns)

    def test_migration_requires_backup_and_backfills_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_legacy_v9(root)
            backup = root / "before-v10.db"
            report = migrate_schema(database, backup)
            self.assertEqual(report.source_version, 9)
            self.assertEqual(report.target_version, SCHEMA_VERSION)
            self.assertTrue(backup.is_file())
            store = CanonicalStore(database)
            with store.connect() as connection:
                categories = dict(connection.execute(
                    "SELECT source_id,source_category FROM conversation_sources"
                ).fetchall())
            self.assertEqual(categories["source-conversation"], "conversation")
            self.assertEqual(categories["source-rss"], "external_info")

    def test_migration_failure_rolls_back_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_legacy_v9(root)
            backup = root / "before-failed-v10.db"

            def fail(point: str) -> None:
                if point == "after_backfill":
                    raise RuntimeError("injected failure")

            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                migrate_schema(database, backup, failure_hook=fail)
            self.assertTrue(backup.is_file())
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "9",
                )
                columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(conversation_sources)"
                )}
            self.assertNotIn("source_category", columns)

    def test_backup_restores_only_to_isolated_new_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_legacy_v9(root)
            backup = root / "before-v10.db"
            report = migrate_schema(database, backup)
            restore = root / "restored-v9.db"
            result = restore_schema_backup(backup, restore)
            self.assertEqual(result["schema_version"], 9)
            self.assertEqual(result["sha256"], report.backup_sha256)
            with self.assertRaises(Exception):
                restore_schema_backup(backup, restore)

    def test_cli_migrates_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self._make_legacy_v9(root)
            backup = root / "cli-backup.db"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    migrate_cli_main([
                        "migrate", "--database", str(database), "--backup", str(backup)
                    ]),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["target_version"], SCHEMA_VERSION)
            restore = root / "cli-restored.db"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    migrate_cli_main([
                        "restore-isolated", "--backup", str(backup),
                        "--destination", str(restore),
                    ]),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["schema_version"], 9)


class TestWorkerPathIntegration(unittest.TestCase):
    def test_worker_uses_legacy_data_dir_alias_without_hardcoded_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = dict(os.environ)
            try:
                os.environ.pop("MIMIR_DATA_DIR", None)
                os.environ.pop("MIMIR_ENV", None)
                os.environ["MIMIR_V8_DATA_DIR"] = tmp
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = worker_main(["review-reminder"])
                self.assertEqual(rc, 0)
                self.assertTrue((Path(tmp) / "canonical.db").is_file())
                self.assertEqual(json.loads(output.getvalue())["total"], 0)
            finally:
                os.environ.clear()
                os.environ.update(saved)

    def test_production_worker_fails_closed_without_explicit_paths(self):
        saved = dict(os.environ)
        try:
            for key in tuple(os.environ):
                if key.startswith("MIMIR_"):
                    os.environ.pop(key, None)
            os.environ["MIMIR_ENV"] = "production"
            with self.assertRaises(Exception):
                worker_main(["review-reminder"])
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
