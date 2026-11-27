# -*- coding: utf-8 -*-
"""1.9.3: ``worker source-purge`` cleans up what one connector produced.

Dry run lists only; --execute rejects that connector's open candidates and
schedules its conversation sources for deletion. Other connectors are untouched.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.candidates import CandidateService, CreateCandidate
from mimir_v8.learning import ConversationEnvelope, ConversationMessage, LearningService
from mimir_v8.schema import reset_dynamic_registry
from mimir_v8.source_purge import purge, survey
from mimir_v8.store import CanonicalStore


def _ingest(store: CanonicalStore, connector_id: str, key: str) -> str:
    env = ConversationEnvelope(
        connector_type="external_agent", connector_id=connector_id,
        session_id=None, owner_principal="mentor",
        memory_mode="observe", retention_class="short",
        messages=(ConversationMessage(role="user", content=f"source purge sample {key}"),),
        source_uri=None, title=key, idempotency_key=key,
    )
    return LearningService(store).ingest_conversation(env, "service:test")["source_id"]


def _candidate(store: CanonicalStore, source_id: str, key: str) -> str:
    return CandidateService(store).create_candidate(CreateCandidate(
        content=f"candidate from {key}", proposed_owner_principal="mentor",
        proposed_domain="knowledge", proposed_fact_type="learning",
        source_id=source_id, idempotency_key=f"cand-{key}"), "service:test")["candidate_id"]


class TestSourcePurge(unittest.TestCase):
    def setUp(self):
        reset_dynamic_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self.tmp.name) / "canon.db")
        self.mira = [_ingest(self.store, "mirasim-x1e", f"m{i}") for i in range(2)]
        self.thor = _ingest(self.store, "thor-bridge", "t1")
        self.mira_cand = _candidate(self.store, self.mira[0], "m0")
        self.thor_cand = _candidate(self.store, self.thor, "t1")

    def tearDown(self):
        reset_dynamic_registry()
        self.tmp.cleanup()

    def _status(self, candidate_id: str) -> str:
        with closing(self.store.connect()) as c:
            return c.execute("SELECT status FROM candidate_facts WHERE candidate_id=?",
                             (candidate_id,)).fetchone()[0]

    def _jobs(self) -> set[str]:
        with closing(self.store.connect()) as c:
            return {r[0] for r in c.execute("SELECT resource_id FROM retention_jobs").fetchall()}

    def test_survey_scopes_to_connector(self):
        s = survey(self.store, "mirasim-x1e")
        self.assertEqual(sorted(s["sources"]), sorted(self.mira))
        self.assertEqual(s["open_candidates"], [self.mira_cand])
        self.assertEqual(s["message_count"], 2)

    def test_dry_run_changes_nothing(self):
        before = self._status(self.mira_cand)
        r = purge(self.store, "mirasim-x1e", reason="dry run", actor="admin", execute=False)
        self.assertEqual(r["counts"]["open_candidates"], 1)
        self.assertEqual(r["rejected"], [])
        self.assertEqual(self._status(self.mira_cand), before)
        self.assertEqual(self._jobs(), set())

    def test_execute_rejects_and_schedules_only_that_connector(self):
        r = purge(self.store, "mirasim-x1e", reason="captured wrong room", actor="admin", execute=True)
        self.assertEqual(r["failed"], [])
        self.assertEqual(r["rejected"], [self.mira_cand])
        self.assertEqual(self._status(self.mira_cand), "rejected")
        self.assertNotEqual(self._status(self.thor_cand), "rejected")
        self.assertEqual(self._jobs(), set(self.mira))

    def test_execute_is_idempotent(self):
        purge(self.store, "mirasim-x1e", reason="r", actor="admin", execute=True)
        again = purge(self.store, "mirasim-x1e", reason="r", actor="admin", execute=True)
        self.assertEqual(again["failed"], [])
        self.assertEqual(again["counts"]["open_candidates"], 0)
        self.assertEqual(sorted(again["already_scheduled"]), sorted(self.mira))
        self.assertEqual(self._jobs(), set(self.mira))

    def test_unknown_connector_is_empty(self):
        r = purge(self.store, "nobody", reason="r", actor="admin", execute=True)
        self.assertEqual(r["counts"], {"sources": 0, "messages": 0, "open_candidates": 0, "committed_facts": 0})


if __name__ == "__main__":
    unittest.main()
