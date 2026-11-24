# -*- coding: utf-8 -*-
"""Extraction owner guard (2026-11-24).

Production case: thor-bridge conversations were ingested with
owner_principal='thor' before thor was in the agent roster. Every
keep-worthy run then raised ValidationError("invalid owner_principal: thor")
inside CreateFact.validated(), was retried every round, and burned an LLM
call each time. Discard verdicts never reached the check, so the failure
looked intermittent.

Pinned behaviour:
1. unregistered owner -> no LLM call, ONE failed(unregistered_owner) row,
   surfaced in ``failed`` (exit 1, audit P1-11 stays intact);
2. later rounds park the run silently (no new row, exit 0) and count it;
3. the run stays 'stored', so registering the owner un-parks it.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.evaluator import Evaluator
from mimir_v8.learning import ConversationEnvelope, ConversationMessage, LearningService
from mimir_v8.schema import register_agent, reset_dynamic_registry
from mimir_v8.store import CanonicalStore
from mimir_v8.worker import _exit_code, llm_extract_once

GHOST = "ghostagent"


def _ingest_as(store: CanonicalStore, owner: str) -> str:
    env = ConversationEnvelope(
        connector_type="hermes_cdc", connector_id="test",
        session_id=None, owner_principal="mentor",
        memory_mode="observe", retention_class="standard",
        messages=(ConversationMessage(role="user", content="我偏好把部署脚本放在 /opt 下统一管理"),),
        source_uri="https://example.com", title="owner-guard",
        idempotency_key=f"owner-guard:{owner}",
    )
    LearningService(store).ingest_conversation(env, "service:test")
    with store.transaction() as c:
        c.execute("UPDATE conversation_sources SET owner_principal=?", (owner,))
        return c.execute("SELECT run_id FROM ingestion_runs LIMIT 1").fetchone()[0]


class TestUnregisteredOwnerGuard(unittest.TestCase):
    def setUp(self):
        reset_dynamic_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self.tmp.name) / "canon.db")
        self.run_id = _ingest_as(self.store, GHOST)

    def tearDown(self):
        reset_dynamic_registry()
        self.tmp.cleanup()

    def _rows(self):
        with contextlib.closing(self.store.connect()) as c:
            return c.execute(
                "SELECT status, error_code FROM extraction_runs WHERE run_id=?", (self.run_id,)
            ).fetchall()

    def _ingestion_status(self):
        with contextlib.closing(self.store.connect()) as c:
            return c.execute("SELECT status FROM ingestion_runs WHERE run_id=?", (self.run_id,)).fetchone()[0]

    def test_first_round_fails_loudly_without_llm(self):
        with mock.patch.object(Evaluator, "evaluate", side_effect=AssertionError("LLM must not be called")) as ev:
            result = llm_extract_once(self.store, "system:test", limit=10)
        ev.assert_not_called()
        self.assertEqual([f["error"] for f in result["failed"]], ["unregistered_owner"])
        self.assertEqual(result["failed"][0]["owner"], GHOST)
        self.assertEqual(_exit_code(result), 1, "P1-11: a new failure must still exit 1")
        self.assertEqual([tuple(r) for r in self._rows()], [("failed", "unregistered_owner")])
        self.assertEqual(self._ingestion_status(), "stored")

    def test_later_rounds_park_silently(self):
        with mock.patch.object(Evaluator, "evaluate", side_effect=AssertionError("no LLM")):
            llm_extract_once(self.store, "system:test", limit=10)
            second = llm_extract_once(self.store, "system:test", limit=10)
        self.assertEqual(second["failed"], [])
        self.assertEqual(second["parked_unregistered_owner"], {GHOST: 1})
        self.assertEqual(_exit_code(second), 0, "already-reported run must not page every round")
        self.assertEqual(len(self._rows()), 1, "no duplicate failure rows")

    def test_registering_owner_unparks_run(self):
        with mock.patch.object(Evaluator, "evaluate", side_effect=AssertionError("no LLM")):
            llm_extract_once(self.store, "system:test", limit=10)
        register_agent(GHOST)
        with mock.patch.object(Evaluator, "evaluate", side_effect=RuntimeError("reached evaluator")) as ev:
            result = llm_extract_once(self.store, "system:test", limit=10)
        ev.assert_called_once()
        self.assertEqual(result["parked_unregistered_owner"], {})
        self.assertEqual([f["error"] for f in result["failed"]], ["RuntimeError"])

    def test_registered_owner_unaffected(self):
        with self.store.transaction() as c:
            c.execute("UPDATE conversation_sources SET owner_principal='mentor'")
        with mock.patch.object(Evaluator, "evaluate", side_effect=RuntimeError("reached evaluator")) as ev:
            result = llm_extract_once(self.store, "system:test", limit=10)
        ev.assert_called_once()
        self.assertEqual(result["parked_unregistered_owner"], {})
        self.assertFalse(any(r["error_code"] == "unregistered_owner" for r in map(dict, self._rows())))


if __name__ == "__main__":
    unittest.main()
