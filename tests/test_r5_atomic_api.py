"""R5 atomic extraction, API, idempotency, and authorization gates."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.candidates import CandidateService, CreateCandidate
from mimir_v8.extraction import EvidenceInput, ExtractionService
from mimir_v8.learning import ConversationEnvelope, ConversationMessage, LearningService
from mimir_v8.query import QueryKernel
from mimir_v8.store import CanonicalStore, ConflictError, new_id


class R5Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.store = CanonicalStore(root / "canonical.db")
        self.extraction = ExtractionService(self.store)

    def ingest(self, connector_type: str, content: str, owner: str = "mentor") -> dict:
        return LearningService(self.store).ingest_conversation(
            ConversationEnvelope(
                connector_type=connector_type,
                connector_id="r5-test",
                session_id=None,
                owner_principal=owner,
                memory_mode="observe",
                retention_class="standard",
                messages=(ConversationMessage(role="user", content=content),),
                source_uri="https://example.invalid/r5",
                title="R5 test",
                idempotency_key=f"r5-ingest:{new_id()}",
            ),
            f"service:{owner}",
        )

    def message_id(self, source_id: str) -> str:
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT message_id FROM conversation_messages WHERE source_id=? ORDER BY ordinal LIMIT 1",
                (source_id,),
            ).fetchone()[0]

    def snapshot(self) -> dict:
        tables = (
            "sources",
            "conversation_sources",
            "conversation_messages",
            "ingestion_runs",
            "extraction_runs",
            "candidate_facts",
            "candidate_evidence",
            "memory_events",
            "outbox",
            "audit_log",
        )
        with self.store.connect() as connection:
            result = {}
            for table in tables:
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                ordering = ",".join(columns) if columns else "rowid"
                result[table] = [tuple(row) for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY {ordering}"
                ).fetchall()]
            return result

    def extract(self, source: dict, *, key: str, evidence_count: int = 2, content: str = "atomic fact", failure_hook=None):
        message_id = self.message_id(source["source_id"])
        evidence = tuple(
            EvidenceInput(
                source_id=source["source_id"],
                message_id=message_id,
                quote_text=f"evidence-{index}",
            )
            for index in range(1, evidence_count + 1)
        )
        return self.extraction.extract_candidate(
            run_id=source["run_id"],
            source_id=source["source_id"],
            actor_principal="mentor",
            content=content,
            owner_principal="mentor",
            domain="knowledge",
            fact_type="reference",
            idempotency_key=key,
            evidence=evidence,
            policy_version="r5-test",
            failure_hook=failure_hook,
        )

    def client(self, *, principal: str = "mentor", scopes=("ingest",), admin: bool = False):
        token = f"r5-token-{new_id()}"
        token_path = self.root / f"tokens-{new_id()}.json"
        token_path.write_text(
            json.dumps({
                "principals": [{
                    "id": principal,
                    "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "scopes": list(scopes),
                    "roles": [],
                    "admin": admin,
                }]
            }),
            encoding="utf-8",
        )
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(token_path),
            query=QueryKernel(self.store),
            extraction=self.extraction,
        )
        return TestClient(create_app(context), raise_server_exceptions=False), token

    def api_body(self, main: dict, evidence_source: dict, *, owner="mentor", key=None):
        return {
            "run_id": main["run_id"],
            "source_id": main["source_id"],
            "content": "API fact",
            "owner_principal": owner,
            "domain": "knowledge",
            "fact_type": "reference",
            "idempotency_key": key or f"r5-api:{new_id()}",
            "evidence": [{
                "source_id": evidence_source["source_id"],
                "message_id": self.message_id(evidence_source["source_id"]),
                "quote_text": "API evidence",
            }],
            "policy_version": "r5-api-test",
        }


class TestR5AtomicRollback(unittest.TestCase):
    FAILURE_POINTS = (
        "after_evidence_preflight",
        "after_candidate_write",
        "after_extraction_insert",
        "before_evidence_1",
        "after_evidence_1",
        "before_evidence_2",
        "after_evidence_2",
        "before_completion",
        "after_completion",
    )

    def test_every_injected_failure_rolls_back_all_tables(self):
        for point in self.FAILURE_POINTS:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                fixture = R5Fixture(Path(tmp))
                source = fixture.ingest("hermes_cdc", "remember this")
                before = fixture.snapshot()

                def hook(current):
                    if current == point:
                        raise RuntimeError(f"injected:{point}")

                with self.assertRaisesRegex(RuntimeError, f"injected:{point}"):
                    fixture.extract(source, key=f"r5-fault:{point}", failure_hook=hook)
                self.assertEqual(before, fixture.snapshot(), f"state drift at {point}")

    def test_success_commits_complete_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            result = fixture.extract(source, key="r5-success")
            self.assertEqual("completed", result["status"])
            self.assertEqual(2, len(result["evidence"]))
            with fixture.store.connect() as connection:
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0])
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM candidate_evidence").fetchone()[0])
                self.assertEqual("completed", connection.execute("SELECT status FROM extraction_runs").fetchone()[0])
                self.assertEqual("extracted", connection.execute(
                    "SELECT status FROM ingestion_runs WHERE run_id=?", (source["run_id"],)
                ).fetchone()[0])

    def test_identical_idempotent_replay_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            first = fixture.extract(source, key="r5-replay")
            before = fixture.snapshot()
            second = fixture.extract(source, key="r5-replay")
            self.assertEqual(before, fixture.snapshot())
            self.assertEqual(first["extraction_id"], second["extraction_id"])
            self.assertTrue(second["idempotent_replay"])

    def test_idempotency_conflict_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            fixture.extract(source, key="r5-conflict", content="first content")
            before = fixture.snapshot()
            with self.assertRaises(ConflictError):
                fixture.extract(source, key="r5-conflict", content="different content")
            self.assertEqual(before, fixture.snapshot())

    def test_idempotency_conflict_on_evidence_and_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            fixture.extract(source, key="r5-contract", evidence_count=1)
            before = fixture.snapshot()
            with self.assertRaises(ConflictError):
                fixture.extract(source, key="r5-contract", evidence_count=2)
            self.assertEqual(before, fixture.snapshot())

    def test_old_key_replay_uses_exact_extraction_after_newer_run_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            first = fixture.extract(source, key="r5-old-key", content="first candidate")
            newer = fixture.extract(source, key="r5-new-key", content="newer candidate")
            self.assertNotEqual(first["extraction_id"], newer["extraction_id"])
            before = fixture.snapshot()
            replay = fixture.extract(source, key="r5-old-key", content="first candidate")
            self.assertEqual(before, fixture.snapshot())
            self.assertEqual(first["extraction_id"], replay["extraction_id"])

    def test_legacy_replay_without_extraction_id_fails_closed_when_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            message_id = fixture.message_id(source["source_id"])
            evidence = (
                EvidenceInput(source_id=source["source_id"], message_id=message_id, quote_text="evidence-1"),
                EvidenceInput(source_id=source["source_id"], message_id=message_id, quote_text="evidence-2"),
            )
            legacy_fingerprint = fixture.extraction._request_fingerprint(
                run_id=source["run_id"], source_id=source["source_id"], actor_principal="mentor",
                content="legacy candidate", owner_principal="mentor", domain="knowledge",
                fact_type="reference", summary=None, evidence=evidence, policy_version="r5-test",
            )
            with fixture.store.transaction() as connection:
                legacy_candidate = CandidateService(fixture.store).create_candidate_in_transaction(
                    connection,
                    CreateCandidate(
                        content="legacy candidate", proposed_owner_principal="mentor",
                        proposed_domain="knowledge", proposed_fact_type="reference",
                        source_id=source["source_id"],
                        source_hash=connection.execute(
                            "SELECT content_hash FROM sources WHERE source_id=?", (source["source_id"],)
                        ).fetchone()[0],
                        uncertainty_reasons=("automatic_extraction_requires_review",),
                        idempotency_key="r5-legacy", idempotency_fingerprint=legacy_fingerprint,
                    ),
                    "mentor",
                )
                for extraction_id in (new_id(), new_id()):
                    connection.execute(
                        "INSERT INTO extraction_runs(extraction_id,run_id,extractor_principal,policy_version,status,candidate_count,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                        (extraction_id, source["run_id"], "mentor", "legacy", "completed", 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
                    )
            self.assertFalse(legacy_candidate.get("extraction_id"))
            before = fixture.snapshot()
            with self.assertRaisesRegex(ConflictError, "legacy idempotency replay is ambiguous"):
                fixture.extract(source, key="r5-legacy", content="legacy candidate")
            self.assertEqual(before, fixture.snapshot())

    def test_replay_without_matching_completed_extraction_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            message_id = fixture.message_id(source["source_id"])
            evidence = (
                EvidenceInput(source_id=source["source_id"], message_id=message_id, quote_text="evidence-1"),
                EvidenceInput(source_id=source["source_id"], message_id=message_id, quote_text="evidence-2"),
            )
            fingerprint = fixture.extraction._request_fingerprint(
                run_id=source["run_id"], source_id=source["source_id"], actor_principal="mentor",
                content="orphan candidate", owner_principal="mentor", domain="knowledge",
                fact_type="reference", summary=None, evidence=evidence, policy_version="r5-test",
            )
            with fixture.store.transaction() as connection:
                CandidateService(fixture.store).create_candidate_in_transaction(
                    connection,
                    CreateCandidate(
                        content="orphan candidate", proposed_owner_principal="mentor",
                        proposed_domain="knowledge", proposed_fact_type="reference",
                        source_id=source["source_id"],
                        source_hash=connection.execute(
                            "SELECT content_hash FROM sources WHERE source_id=?", (source["source_id"],)
                        ).fetchone()[0],
                        uncertainty_reasons=("automatic_extraction_requires_review",),
                        idempotency_key="r5-orphan", idempotency_fingerprint=fingerprint,
                        extraction_id=new_id(),
                    ),
                    "mentor",
                )
            before = fixture.snapshot()
            with self.assertRaisesRegex(ConflictError, "no matching completed extraction"):
                fixture.extract(source, key="r5-orphan", content="orphan candidate")
            self.assertEqual(before, fixture.snapshot())

    def test_concurrent_same_key_produces_one_committed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            source = fixture.ingest("hermes_cdc", "remember this")
            results = []
            errors = []
            barrier = threading.Barrier(2)

            def run():
                try:
                    barrier.wait()
                    results.append(fixture.extract(source, key="r5-concurrent"))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)
            self.assertEqual(2, len(results))
            self.assertEqual(1, len({item["candidate"]["candidate_id"] for item in results}))
            with fixture.store.connect() as connection:
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0])
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM candidate_evidence").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0])


class TestR5RealAPI(unittest.TestCase):
    def _assert_rejected_unchanged(self, connector_type: str, expected_status: int = 422):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            main = fixture.ingest("hermes_cdc", "remember")
            other = fixture.ingest(connector_type, "other content")
            client, token = fixture.client()
            before = fixture.snapshot()
            response = client.post(
                "/v8/learning/extractions",
                json=fixture.api_body(main, other),
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(expected_status, response.status_code, response.text)
            self.assertEqual("validation_error", response.json()["error"]["code"])
            self.assertEqual(before, fixture.snapshot())

    def test_rss_evidence_rejected(self):
        self._assert_rejected_unchanged("rss")

    def test_unknown_evidence_rejected(self):
        self._assert_rejected_unchanged("unknown-source")

    def test_knowledge_doc_evidence_rejected(self):
        self._assert_rejected_unchanged("file")

    def test_cross_conversation_evidence_rejected(self):
        self._assert_rejected_unchanged("workbuddy")

    def test_same_source_success_returns_201(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            main = fixture.ingest("hermes_cdc", "remember")
            client, token = fixture.client()
            response = client.post(
                "/v8/learning/extractions",
                json=fixture.api_body(main, main),
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(201, response.status_code, response.text)
            self.assertEqual("completed", response.json()["status"])

    def test_missing_scope_is_403_and_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            main = fixture.ingest("hermes_cdc", "remember")
            client, token = fixture.client(scopes=("read",))
            before = fixture.snapshot()
            response = client.post(
                "/v8/learning/extractions",
                json=fixture.api_body(main, main),
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(403, response.status_code, response.text)
            self.assertEqual("missing_scope", response.json()["error"]["code"])
            after = fixture.snapshot()
            before_audit, after_audit = before.pop("audit_log"), after.pop("audit_log")
            self.assertEqual(before, after)
            # P0-3: security denials intentionally append one audit trail row.
            self.assertEqual(len(after_audit), len(before_audit) + 1)

    def test_owner_boundary_is_403_and_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            main = fixture.ingest("hermes_cdc", "remember")
            client, token = fixture.client(principal="other", scopes=("ingest",))
            before = fixture.snapshot()
            response = client.post(
                "/v8/learning/extractions",
                json=fixture.api_body(main, main, owner="mentor"),
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(403, response.status_code, response.text)
            self.assertEqual("owner_boundary", response.json()["error"]["code"])
            after = fixture.snapshot()
            before_audit, after_audit = before.pop("audit_log"), after.pop("audit_log")
            self.assertEqual(before, after)
            # P0-3: security denials intentionally append one audit trail row.
            self.assertEqual(len(after_audit), len(before_audit) + 1)

    def test_api_midflow_fault_returns_500_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R5Fixture(Path(tmp))
            main = fixture.ingest("hermes_cdc", "remember")
            client, token = fixture.client()
            before = fixture.snapshot()

            def failure(_failure_hook, point):
                if point == "after_evidence_1":
                    raise RuntimeError("api-midflow-fault")

            fixture.extraction._failure = failure
            response = client.post(
                "/v8/learning/extractions",
                json=fixture.api_body(main, main),
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(500, response.status_code, response.text)
            self.assertEqual("internal_error", response.json()["error"]["code"])
            self.assertEqual(before, fixture.snapshot())


if __name__ == "__main__":
    unittest.main()
