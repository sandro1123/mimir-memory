# -*- coding: utf-8 -*-
"""1.9.0 closeout (2026-11-24): extraction-side fixes.

1. Bridge wrappers (<bridge_context>/<bridge_instructions>/<interactive_cards>/
   <tool_call>/<system-reminder>) never reach the extraction model; the
   <user_input> JSON envelope is unwrapped to its ``text``. Stored evidence in
   conversation_messages is left untouched.
2. Runs parked for an unregistered owner (already reported once) must not take
   LIMIT slots: with parked >= limit, new conversations would starve forever.
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
from mimir_v8.schema import reset_dynamic_registry
from mimir_v8.store import CanonicalStore
from mimir_v8.worker import build_extraction_context, llm_extract_once, strip_bridge_wrappers

BRIDGE_MSG = (
    "<bridge_context>\n{\"chatId\":\"oc_x\",\"senderId\":\"ou_y\"}\n</bridge_context>\n\n"
    "<bridge_instructions>\n[\"不要重启 aiduCAT\"]\n</bridge_instructions>\n\n"
    "<user_input>\n{\"text\":\"1.9.0还没修复好没发版呀\"}\n</user_input>"
)


class TestStripBridgeWrappers(unittest.TestCase):
    def test_full_envelope_reduced_to_user_text(self):
        self.assertEqual(strip_bridge_wrappers(BRIDGE_MSG), "1.9.0还没修复好没发版呀")

    def test_plain_text_untouched(self):
        for s in ("普通文本", "a < b and c > d", "用 <div> 包一下", ""):
            self.assertEqual(strip_bridge_wrappers(s), s)

    def test_truncated_open_block_dropped(self):
        self.assertEqual(strip_bridge_wrappers("前文<bridge_instructions>[\"截断"), "前文")

    def test_non_json_user_input_kept_verbatim(self):
        self.assertEqual(strip_bridge_wrappers("<user_input>直接说的话</user_input>"), "直接说的话")

    def test_other_drop_tags(self):
        s = "看这个<interactive_cards>[{}]</interactive_cards><tool_call>{\"name\":\"Bash\"}</tool_call>结束"
        self.assertEqual(strip_bridge_wrappers(s), "看这个结束")

    def test_context_has_no_wrappers_and_wrapper_only_message_skipped(self):
        ctx = build_extraction_context([
            {"role": "user", "content_redacted": BRIDGE_MSG},
            {"role": "user", "content_redacted": "<bridge_context>{}</bridge_context>"},
            {"role": "assistant", "content_redacted": "好的，先修 1.9.0"},
        ])
        self.assertEqual(ctx, "[User]: 1.9.0还没修复好没发版呀\n[Assistant]: 好的，先修 1.9.0")
        self.assertNotIn("bridge_", ctx)


def _ingest(store: CanonicalStore, owner: str, key: str, text: str) -> str:
    env = ConversationEnvelope(
        connector_type="hermes_cdc", connector_id="test",
        session_id=None, owner_principal="mentor",
        memory_mode="observe", retention_class="standard",
        messages=(ConversationMessage(role="user", content=text),),
        source_uri="https://example.com", title=key, idempotency_key=key,
    )
    LearningService(store).ingest_conversation(env, "service:test")
    with store.transaction() as c:
        source_id = c.execute(
            "SELECT source_id FROM conversation_sources WHERE title=?", (key,)).fetchone()[0]
        c.execute("UPDATE conversation_sources SET owner_principal=? WHERE source_id=?", (owner, source_id))
        return c.execute("SELECT run_id FROM ingestion_runs WHERE source_id=?", (source_id,)).fetchone()[0]


class TestParkedRunsDoNotStarve(unittest.TestCase):
    def setUp(self):
        reset_dynamic_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self.tmp.name) / "canon.db")
        self.ghost_runs = [
            _ingest(self.store, "ghostagent", f"ghost-{i}", f"我偏好第{i}种部署方式，放在 /opt 下")
            for i in range(3)
        ]

    def tearDown(self):
        reset_dynamic_registry()
        self.tmp.cleanup()

    def test_new_run_reached_when_parked_exceeds_limit(self):
        with mock.patch.object(Evaluator, "evaluate", side_effect=AssertionError("no LLM")):
            first = llm_extract_once(self.store, "system:test", limit=10)
        self.assertEqual(len(first["failed"]), 3)

        fresh = _ingest(self.store, "mentor", "fresh", "我偏好用 systemd timer 而不是 cron")
        with mock.patch.object(Evaluator, "evaluate", side_effect=RuntimeError("reached evaluator")) as ev:
            result = llm_extract_once(self.store, "system:test", limit=2)
        ev.assert_called_once()
        self.assertEqual([f["run_id"] for f in result["failed"]], [fresh])
        self.assertEqual(result["parked_unregistered_owner"], {"ghostagent": 3})

    def test_parked_only_queue_reports_counts(self):
        with mock.patch.object(Evaluator, "evaluate", side_effect=AssertionError("no LLM")):
            llm_extract_once(self.store, "system:test", limit=10)
            again = llm_extract_once(self.store, "system:test", limit=10)
        self.assertEqual(again["failed"], [])
        self.assertEqual(again["parked_unregistered_owner"], {"ghostagent": 3})

    def test_wrappers_not_sent_to_evaluator(self):
        _ingest(self.store, "mentor", "bridged", BRIDGE_MSG)
        with self.store.transaction() as c:
            c.execute("UPDATE conversation_sources SET owner_principal='mentor'")
        seen = []
        with mock.patch.object(Evaluator, "evaluate",
                               side_effect=lambda text: seen.append(text) or (_ for _ in ()).throw(RuntimeError("stop"))):
            llm_extract_once(self.store, "system:test", limit=10)
        self.assertTrue(seen)
        for text in seen:
            self.assertNotIn("bridge_", text)
        self.assertIn("1.9.0还没修复好没发版呀", "\n".join(seen))
        with contextlib.closing(self.store.connect()) as c:
            stored = [r[0] for r in c.execute("SELECT content_redacted FROM conversation_messages")]
        self.assertTrue(any("<bridge_context>" in s for s in stored), "stored evidence must stay verbatim")


if __name__ == "__main__":
    unittest.main()
