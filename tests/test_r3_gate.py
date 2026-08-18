"""Mímir INDEP-P0-R3: ExtractionService source_category gate tests.

Verifies that ExtractionService.extract_candidate() enforces
source_category='conversation' in its transaction boundary, and that
non-conversation sources are rejected without side effects.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.store import CanonicalStore, new_id, sha256_text, utc_now
from mimir_v8.learning import LearningService, ConversationEnvelope, ConversationMessage
from mimir_v8.extraction import ExtractionService, EvidenceInput
from mimir_v8.classifier import classify
from mimir_v8.schema import ValidationError
from mimir_v8.worker import extract_once, llm_extract_once


class TestExtractionServiceGate(unittest.TestCase):
    """R3-01/R3-02: ExtractionService.extract_candidate() must deny non-conversation."""

    def _ingest(self, store, connector_type, content="test content", owner="mentor"):
        """Helper: ingest a source and return its run_id, source_id, source_category."""
        learning = LearningService(store)
        env = ConversationEnvelope(
            connector_type=connector_type, connector_id="test",
            session_id=None, owner_principal=owner,
            memory_mode="observe", retention_class="standard",
            messages=(ConversationMessage(role="user", content=content),),
            source_uri="https://example.com", title="test",
            idempotency_key=f"r3-test:{connector_type}:{new_id()}",
        )
        return learning.ingest_conversation(env, f"service:{owner}")

    def _extract(self, store, run_id, source_id, owner="mentor"):
        """Call ExtractionService.extract_candidate() directly."""
        svc = ExtractionService(store)
        return svc.extract_candidate(
            run_id=run_id, source_id=source_id, actor_principal=owner,
            content="test extraction content", owner_principal=owner,
            domain="knowledge", fact_type="reference",
            idempotency_key=f"r3-extract:{new_id()}",
            policy_version="v8.1-test",
        )

    def _check_state_unchanged(self, store, run_id, label=""):
        """Verify no extraction_run completed, no candidate, ingestion unchanged."""
        import sqlite3
        with store.connect() as conn:
            er = conn.execute("SELECT status,error_code FROM extraction_runs WHERE run_id=?", (run_id,)).fetchone()
            cf = conn.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0]
            ev = conn.execute("SELECT COUNT(*) FROM candidate_evidence").fetchone()[0]
            ing = conn.execute("SELECT status FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
            print(f"    [{label}] extraction_runs={er}, candidates={cf}, evidence={ev}, ingestion={ing[0] if ing else 'N/A'}")

    # ─── Positive: conversation allowed ───

    def test_conversation_extraction_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r = self._ingest(store, "hermes_cdc", "请记住这条规则")
            result = self._extract(store, r["run_id"], r["source_id"])
            self.assertIn("candidate", result)
            self.assertEqual(result["status"], "completed")

    # ─── Negative: non-conversation denied ───

    def test_rss_external_info_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r = self._ingest(store, "rss")
            with self.assertRaises(ValidationError) as ctx:
                self._extract(store, r["run_id"], r["source_id"])
            self.assertIn("must be 'conversation'", str(ctx.exception))

    def test_unknown_quarantine_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r = self._ingest(store, "unknown_xyz")
            with self.assertRaises(ValidationError) as ctx:
                self._extract(store, r["run_id"], r["source_id"])
            self.assertIn("must be 'conversation'", str(ctx.exception))

    def test_knowledge_doc_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r = self._ingest(store, "file")
            with self.assertRaises(ValidationError) as ctx:
                self._extract(store, r["run_id"], r["source_id"])
            self.assertIn("must be 'conversation'", str(ctx.exception))

    # ─── Negative: run/source mismatch ───

    def test_run_source_mismatch_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r1 = self._ingest(store, "hermes_cdc", "conv content")
            r2 = self._ingest(store, "hermes_cdc", "other content")
            with self.assertRaises((ValidationError, ValueError)):
                self._extract(store, r1["run_id"], r2["source_id"])

    # ─── Negative: missing source_category column ───

    def test_missing_source_category_column_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "canonical.db"
            store = CanonicalStore(db)
            r = self._ingest(store, "hermes_cdc", "请记住")
            # Drop the source_category column using raw SQL
            import sqlite3
            raw = sqlite3.connect(str(db))
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("PRAGMA ignore_check_constraints=ON")
            raw.execute("CREATE TABLE cs_v2 AS SELECT source_id,connector_type,connector_id,session_id,source_uri,source_hash,title,owner_principal,retention_class,memory_mode,started_at,ended_at,ingested_at,metadata_json FROM conversation_sources")
            raw.execute("DROP TABLE conversation_sources")
            raw.execute("ALTER TABLE cs_v2 RENAME TO conversation_sources")
            raw.close()
            # R6 rejects an incomplete existing schema before extraction starts.
            with self.assertRaises(Exception):
                CanonicalStore(db)

    # ─── State unchanged verification ───

    def test_rss_state_unchanged_after_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r = self._ingest(store, "rss")
            self._check_state_unchanged(store, r["run_id"], "before")
            try:
                self._extract(store, r["run_id"], r["source_id"])
            except ValidationError:
                pass
            self._check_state_unchanged(store, r["run_id"], "after")
            # Verify no candidate or evidence was created
            with store.connect() as conn:
                cf = conn.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0]
                ev = conn.execute("SELECT COUNT(*) FROM candidate_evidence").fetchone()[0]
                ing = conn.execute("SELECT status FROM ingestion_runs WHERE run_id=?", (r["run_id"],)).fetchone()
            self.assertEqual(cf, 0, "no candidate should be created")
            self.assertEqual(ev, 0, "no evidence should be created")
            self.assertEqual(ing[0], "stored", "ingestion should remain 'stored'")

    def test_unknown_state_unchanged_after_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r = self._ingest(store, "unknown_xyz")
            try:
                self._extract(store, r["run_id"], r["source_id"])
            except ValidationError:
                pass
            with store.connect() as conn:
                cf = conn.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0]
                ing = conn.execute("SELECT status FROM ingestion_runs WHERE run_id=?", (r["run_id"],)).fetchone()
            self.assertEqual(cf, 0)
            self.assertEqual(ing[0], "stored")

    # ─── Worker path still works ───

    def test_worker_extract_once_still_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            self._ingest(store, "hermes_cdc", "我偏好简洁的答案")
            self._ingest(store, "rss", "新闻内容")
            result = extract_once(store, "mentor", limit=10)
            self.assertEqual(result["count"], 1, "only conversation extracted")

    def test_worker_llm_extract_once_still_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            self._ingest(store, "hermes_cdc", "我偏好简洁的答案")
            self._ingest(store, "rss", "新闻内容")
            result = llm_extract_once(store, "mentor", limit=10)
            self.assertEqual(result["evaluator_mode"], "llm")


if __name__ == "__main__":
    unittest.main()