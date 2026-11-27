# -*- coding: utf-8 -*-
"""Extraction evaluator fallback chain (1.9.6).

Governance has walked a fallback chain since 1.9.0; the extraction evaluator
had none, so one primary timeout counted as a failed evaluation. Pins: the
chain is tried in order only while the model returns nothing, the governance
chain is reused when no evaluator chain is set, and model_used names the
model that answered.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.evaluator import Evaluator

BASE = {"MIMIR_EVALUATOR_API_URL": "http://magpie.test/v1", "MIMIR_EVALUATOR_API_KEY": "k",
        "MIMIR_EVALUATOR_MODEL": "m-primary"}
GOOD = json.dumps({"salience": 0.8, "risk": "low", "domain": "quant", "fact_type": "iron_rule",
                   "summary": "回测必须用 PIT 数据", "reasoning": "r", "is_valuable": True})
TEXT = "用户要求回测必须用真实 PIT 数据，禁止前视偏差。"


def make(env):
    with mock.patch.dict("os.environ", env, clear=True):
        return Evaluator()


class TestEvaluatorFallback(unittest.TestCase):
    def test_chain_parsed_and_primary_excluded(self):
        ev = make({**BASE, "MIMIR_EVALUATOR_FALLBACK_MODELS": "m-fb1, m-primary, ,m-fb2"})
        self.assertEqual(ev.fallback_models, ["m-fb1", "m-fb2"])

    def test_reuses_governance_chain(self):
        ev = make({**BASE, "MIMIR_GOVERNANCE_FALLBACK_MODELS": "g-fb1,g-fb2"})
        self.assertEqual(ev.fallback_models, ["g-fb1", "g-fb2"])

    def test_evaluator_chain_wins_over_governance(self):
        ev = make({**BASE, "MIMIR_EVALUATOR_FALLBACK_MODELS": "e-fb",
                   "MIMIR_GOVERNANCE_FALLBACK_MODELS": "g-fb"})
        self.assertEqual(ev.fallback_models, ["e-fb"])

    def test_no_chain_by_default(self):
        self.assertEqual(make(BASE).fallback_models, [])

    def test_primary_success_does_not_touch_chain(self):
        ev = make({**BASE, "MIMIR_EVALUATOR_FALLBACK_MODELS": "m-fb1"})
        calls = []
        with mock.patch.object(ev, "_call_llm", side_effect=lambda m, model=None: calls.append(model) or GOOD):
            res = ev.evaluate(TEXT)
        self.assertEqual(calls, [None])
        self.assertEqual(res.model_used, "m-primary")
        self.assertFalse(res.llm_unavailable)

    def test_walks_chain_until_success(self):
        ev = make({**BASE, "MIMIR_EVALUATOR_FALLBACK_MODELS": "m-fb1,m-fb2"})
        calls = []

        def fake(messages, model=None):
            calls.append(model)
            return GOOD if model == "m-fb2" else None

        with mock.patch.object(ev, "_call_llm", side_effect=fake):
            res = ev.evaluate(TEXT)
        self.assertEqual(calls, [None, "m-fb1", "m-fb2"])
        self.assertEqual(res.model_used, "m-fb2")
        self.assertFalse(res.llm_unavailable)
        self.assertFalse(res.parse_error)

    def test_all_fail_still_marks_unavailable(self):
        ev = make({**BASE, "MIMIR_EVALUATOR_FALLBACK_MODELS": "m-fb1"})
        with mock.patch.object(ev, "_call_llm", return_value=None):
            res = ev.evaluate(TEXT)
        self.assertTrue(res.llm_unavailable)

    def test_parse_error_is_not_retried(self):
        ev = make({**BASE, "MIMIR_EVALUATOR_FALLBACK_MODELS": "m-fb1"})
        calls = []
        with mock.patch.object(ev, "_call_llm", side_effect=lambda m, model=None: calls.append(model) or "not json"):
            res = ev.evaluate(TEXT)
        self.assertEqual(calls, [None])
        self.assertIsNotNone(res.parse_error)

    def test_request_body_uses_fallback_model(self):
        ev = make({**BASE, "MIMIR_EVALUATOR_FALLBACK_MODELS": "m-fb1"})
        sent = []

        def urlopen(req, timeout):
            sent.append(json.loads(req.data)["model"])
            raise TimeoutError("timed out")

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            res = ev.evaluate(TEXT)
        self.assertEqual(sent, ["m-primary", "m-fb1"])
        self.assertTrue(res.llm_unavailable)


if __name__ == "__main__":
    unittest.main()
