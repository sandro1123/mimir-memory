"""Mímir INDEP-P0-R4: Evidence source_category gate tests.

Verifies that ExtractionService.extract_candidate() requires ALL evidence
to match the validated main source_id and have source_category='conversation'.
Every reject scenario asserts full state unchanged across all affected tables.
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
from mimir_v8.schema import ValidationError


class TestEvidenceGate(unittest.TestCase):
    """R4: Evidence must match main source_id and be conversation."""

    def _snapshot(self, store, label=""):
        """Return a dict of all relevant table counts for state comparison."""
        with store.connect() as conn:
            return {
                "label": label,
                "candidate_facts": conn.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0],
                "candidate_evidence": conn.execute("SELECT COUNT(*) FROM candidate_evidence").fetchone()[0],
                "extraction_runs": [(r[0], r[1]) for r in conn.execute(
                    "SELECT extraction_id, status FROM extraction_runs ORDER BY started_at").fetchall()],
                "extraction_runs_count": conn.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0],
                "ingestion_stored": conn.execute("SELECT COUNT(*) FROM ingestion_runs WHERE status='stored'").fetchone()[0],
                "ingestion_extracted": conn.execute("SELECT COUNT(*) FROM ingestion_runs WHERE status='extracted'").fetchone()[0],
                "conversation_sources": conn.execute("SELECT COUNT(*) FROM conversation_sources").fetchone()[0],
                "memory_events": conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0],
            }

    def _assert_snapshots_equal(self, before, after, exclude_keys=None):
        """Assert two snapshots are identical except for label."""
        if exclude_keys is None:
            exclude_keys = {"label", "extraction_runs"}
        for k in before:
            if k in exclude_keys:
                continue
            self.assertEqual(
                before[k], after[k],
                f"state changed: {k} {before[k]} -> {after[k]}"
            )

    def _ingest(self, store, connector_type, content="test content", owner="mentor"):
        learning = LearningService(store)
        env = ConversationEnvelope(
            connector_type=connector_type, connector_id="test",
            session_id=None, owner_principal=owner,
            memory_mode="observe", retention_class="standard",
            messages=(ConversationMessage(role="user", content=content),),
            source_uri="https://example.com", title="test",
            idempotency_key=f"r4:{connector_type}:{new_id()}",
        )
        return learning.ingest_conversation(env, f"service:{owner}")

    def _extract(self, store, run_id, source_id, evidence_items, owner="mentor"):
        svc = ExtractionService(store)
        return svc.extract_candidate(
            run_id=run_id, source_id=source_id, actor_principal=owner,
            content="extraction content", owner_principal=owner,
            domain="knowledge", fact_type="reference",
            idempotency_key=f"r4-extract:{new_id()}",
            evidence=tuple(evidence_items),
            policy_version="v8.1-test",
        )

    # ─── Positive: conversation + same source evidence ───

    def test_conversation_same_source_evidence_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            r = self._ingest(store, "hermes_cdc", "请记住这条规则")
            before = self._snapshot(store, "before")
            result = self._extract(store, r["run_id"], r["source_id"], [
                EvidenceInput(source_id=r["source_id"], message_id=self._get_msg(store, r["source_id"]), quote_text="evidence"),
            ])
            self.assertIn("candidate", result)
            self.assertEqual(result["status"], "completed")

    def _get_msg(self, store, source_id):
        with store.connect() as conn:
            row = conn.execute(
                "SELECT message_id FROM conversation_messages WHERE source_id=? ORDER BY ordinal LIMIT 1",
                (source_id,),
            ).fetchone()
            return row[0] if row else None

    # ─── Negative: conversation main + RSS evidence ───

    def test_conversation_main_rss_evidence_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conv = self._ingest(store, "hermes_cdc", "请记住")
            rss = self._ingest(store, "rss", "news")
            before = self._snapshot(store, "before")
            try:
                self._extract(store, conv["run_id"], conv["source_id"], [
                    EvidenceInput(source_id=rss["source_id"], message_id=self._get_msg(store, rss["source_id"]), quote_text="bad evidence"),
                ])
                self.fail("should have raised ValidationError")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)

    # ─── Negative: conversation main + unknown evidence ───

    def test_conversation_main_unknown_evidence_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conv = self._ingest(store, "hermes_cdc", "请记住")
            unk = self._ingest(store, "unknown_xyz", "unknown")
            before = self._snapshot(store, "before")
            try:
                self._extract(store, conv["run_id"], conv["source_id"], [
                    EvidenceInput(source_id=unk["source_id"], message_id=self._get_msg(store, unk["source_id"]), quote_text="bad ev"),
                ])
                self.fail("should have raised")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)

    # ─── Negative: conversation main + knowledge_doc evidence ───

    def test_conversation_main_knowledge_doc_evidence_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conv = self._ingest(store, "hermes_cdc", "请记住")
            doc = self._ingest(store, "file", "doc content")
            before = self._snapshot(store, "before")
            try:
                self._extract(store, conv["run_id"], conv["source_id"], [
                    EvidenceInput(source_id=doc["source_id"], message_id=self._get_msg(store, doc["source_id"]), quote_text="bad ev"),
                ])
                self.fail("should have raised")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)

    # ─── Negative: conversation main + different conversation source evidence ───

    def test_conversation_main_different_conversation_evidence_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            c1 = self._ingest(store, "hermes_cdc", "请记住规则1")
            c2 = self._ingest(store, "hermes_cdc", "请记住规则2")
            before = self._snapshot(store, "before")
            try:
                self._extract(store, c1["run_id"], c1["source_id"], [
                    EvidenceInput(source_id=c2["source_id"], message_id=self._get_msg(store, c2["source_id"]), quote_text="cross ev"),
                ])
                self.fail("should have raised")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)

    # ─── Negative: evidence source/message mismatch ───

    def test_evidence_source_message_mismatch_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conv = self._ingest(store, "hermes_cdc", "请记住")
            # Get a message from a different source
            rss = self._ingest(store, "rss", "news")
            before = self._snapshot(store, "before")
            try:
                self._extract(store, conv["run_id"], conv["source_id"], [
                    EvidenceInput(source_id=conv["source_id"], message_id=self._get_msg(store, rss["source_id"]), quote_text="mismatch"),
                ])
                self.fail("should have raised")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)

    # ─── Negative: evidence source does not exist ───

    def test_evidence_source_nonexistent_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conv = self._ingest(store, "hermes_cdc", "请记住")
            before = self._snapshot(store, "before")
            try:
                self._extract(store, conv["run_id"], conv["source_id"], [
                    EvidenceInput(source_id="nonexistent-source-id", message_id="nonexistent-msg-id", quote_text="bad ev"),
                ])
                self.fail("should have raised")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)

    # ─── Negative: missing source_category column ───

    def test_missing_source_category_column_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "canonical.db"
            store = CanonicalStore(db)
            conv = self._ingest(store, "hermes_cdc", "请记住")
            before = self._snapshot(store, "before")
            # Drop source_category column
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
            raw = sqlite3.connect(str(db))
            candidate_count = raw.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0]
            raw.close()
            self.assertEqual(candidate_count, before["candidate_facts"])

    # ─── Negative: multi-evidence, second one invalid → full rollback ───

    def test_multi_evidence_second_invalid_full_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conv = self._ingest(store, "hermes_cdc", "请记住")
            rss = self._ingest(store, "rss", "news")
            msg = self._get_msg(store, conv["source_id"])
            before = self._snapshot(store, "before")
            try:
                self._extract(store, conv["run_id"], conv["source_id"], [
                    EvidenceInput(source_id=conv["source_id"], message_id=msg, quote_text="good evidence"),
                    EvidenceInput(source_id=rss["source_id"], message_id=self._get_msg(store, rss["source_id"]), quote_text="bad evidence"),
                ])
                self.fail("should have raised")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)

    # ─── Negative: empty evidence quote ───

    def test_empty_evidence_quote_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            conv = self._ingest(store, "hermes_cdc", "请记住")
            before = self._snapshot(store, "before")
            try:
                self._extract(store, conv["run_id"], conv["source_id"], [
                    EvidenceInput(source_id=conv["source_id"], message_id=self._get_msg(store, conv["source_id"]), quote_text=""),
                ])
                self.fail("should have raised")
            except ValidationError:
                pass
            after = self._snapshot(store, "after")
            self._assert_snapshots_equal(before, after)


if __name__ == "__main__":
    unittest.main()