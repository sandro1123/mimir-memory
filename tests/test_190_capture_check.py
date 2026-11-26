# -*- coding: utf-8 -*-
"""1.9.0 item 8 (2026-11-24): capture-gap alert.

``worker capture-check`` flags watched connectors whose newest
conversation_source is older than the threshold (or that were never seen),
returns status=error so the unit exits 1, and can emit a Feishu card.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.learning import ConversationEnvelope, ConversationMessage, LearningService
from mimir_v8.schema import reset_dynamic_registry
from mimir_v8.store import CanonicalStore
from mimir_v8.worker import _exit_code, _parse_capture_watch, capture_gap_check


def _ingest(store: CanonicalStore, connector_id: str, key: str) -> None:
    env = ConversationEnvelope(
        connector_type="external_agent", connector_id=connector_id,
        session_id=None, owner_principal="mentor",
        memory_mode="observe", retention_class="short",
        messages=(ConversationMessage(role="user", content=f"capture check {key}"),),
        source_uri=None, title=key, idempotency_key=key,
    )
    LearningService(store).ingest_conversation(env, "service:test")


class TestCaptureGapCheck(unittest.TestCase):
    def setUp(self):
        reset_dynamic_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self.tmp.name) / "canon.db")
        _ingest(self.store, "thor-bridge", "t1")

    def tearDown(self):
        reset_dynamic_registry()
        self.tmp.cleanup()

    def test_fresh_connector_ok(self):
        r = capture_gap_check(self.store, {"thor-bridge": 24})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["stale"], [])
        self.assertEqual(_exit_code(r), 0)
        self.assertNotIn("feishu_card", capture_gap_check(self.store, {"thor-bridge": 24}, output="feishu"))

    def test_old_connector_stale_and_exits_1(self):
        later = datetime.now(timezone.utc) + timedelta(hours=30)
        r = capture_gap_check(self.store, {"thor-bridge": 24}, now=later, output="feishu")
        self.assertEqual(r["status"], "error")
        self.assertEqual([e["connector_id"] for e in r["stale"]], ["thor-bridge"])
        self.assertGreater(r["stale"][0]["gap_hours"], 24)
        self.assertEqual(_exit_code(r), 1)
        self.assertIn("thor-bridge", r["feishu_card"]["card"]["elements"][0]["content"])

    def test_never_seen_connector_is_stale(self):
        r = capture_gap_check(self.store, {"thor-bridge": 24, "odin-bridge": 72}, output="feishu")
        self.assertEqual([e["connector_id"] for e in r["stale"]], ["odin-bridge"])
        self.assertIsNone(r["stale"][0]["gap_hours"])
        self.assertIn("从未收到对话", r["feishu_card"]["card"]["elements"][0]["content"])

    def test_default_watch_covers_every_bridge(self):
        from mimir_v8.worker import CAPTURE_WATCH_DEFAULT
        for name in ("thor-bridge", "odin-bridge", "brokkr-bridge", "hermes-jarvis-profile"):
            self.assertIn(name, CAPTURE_WATCH_DEFAULT)

    def test_parse_watch(self):
        self.assertEqual(_parse_capture_watch("a=12, b=48.5,c"), {"a": 12.0, "b": 48.5, "c": 24.0})
        self.assertEqual(_parse_capture_watch(""), {})
        with self.assertRaises(SystemExit):
            _parse_capture_watch("a=x")


if __name__ == "__main__":
    unittest.main()
