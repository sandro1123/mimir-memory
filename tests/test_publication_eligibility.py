# -*- coding: utf-8 -*-
"""Stage-2 Task 2 — the four-gate publish eligibility.

Labels alone do not protect: the production audit (spec §7.7) proved real
keys are stored with ``sensitivity=internal``. A fact may enter the
ObsidianPublisher candidate set only when all four gates pass, in order:

1. ``sensitivity`` is not ``restricted``
2. ``visibility`` is not ``all`` (all = every agent in the vault pool reads it)
3. the body survives credential-shape redaction untouched
4. egress allows external publication (P1: ``local_only`` is refused)

P2 (redact-then-publish) will relax gate 4 for clean bodies later; the gate
order and gate 3 stay identical.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.publication import is_publishable_fact


def _fact(**over):
    base = {
        "sensitivity": "internal",
        "visibility": "shared",
        "egress_policy": "local_only",
    }
    base.update(over)
    return base


class TestPublishEligibility(unittest.TestCase):
    def test_gate_order_and_reasons(self):
        cases = [
            (_fact(sensitivity="restricted"), "ok text", "sensitivity:restricted"),
            (_fact(visibility="all"), "ok text", "visibility:all"),
            (_fact(), "key sk-abcdefghijklmnopqrst", "credential:redacted"),
            (_fact(), "ok text", "egress:local_only"),
            (_fact(egress_policy="external_allowed"), "clean body", "ok"),
        ]
        for fact, body, expected in cases:
            ok, reason = is_publishable_fact(fact, body, target_scope="shared")
            self.assertEqual(reason, expected, msg=f"body={body!r}")
            self.assertIs(ok, expected == "ok")

    def test_gate_3_uses_redact_text_not_new_regex(self):
        """同一份真相：凭据形态判定必须复用 learning.redact_text。"""
        import mimir_v8.publication as pub
        import mimir_v8.learning as learning

        self.assertIs(pub._redact_text, learning.redact_text)

    def test_private_target_scope_relaxes_gate_4(self):
        """target_scope=private 时 local_only 事实可进（不离开本机的目标）。"""
        ok, reason = is_publishable_fact(_fact(), "ok text", target_scope="private")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_empty_body_is_rejected(self):
        ok, reason = is_publishable_fact(_fact(), "", target_scope="shared")
        self.assertFalse(ok)
        self.assertEqual(reason, "empty:body")

    def test_missing_label_defaults_fail_closed(self):
        """缺字段的 dict 不能靠默认猜成可发布。"""
        ok, reason = is_publishable_fact({}, "ok text", target_scope="shared")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
