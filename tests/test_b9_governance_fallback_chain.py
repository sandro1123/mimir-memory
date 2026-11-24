# -*- coding: utf-8 -*-
"""B9: governance fallback chain + configurable timeout (1.9.0).

Magpie free tiers return 429 under load; one fallback model was not enough.
MIMIR_GOVERNANCE_FALLBACK_MODELS is an ordered comma list tried in turn.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mimir_v8.governance as gov

ENV = {
    "MIMIR_ROUTER_URL": "http://magpie.test/v1",
    "MIMIR_ROUTER_API_KEY": "k",
    "MIMIR_GOVERNANCE_MODEL": "m-primary",
    "MIMIR_GOVERNANCE_FALLBACK_MODELS": "m-fb1, m-fb2",
    "MIMIR_GOVERNANCE_TIMEOUT_S": "45",
}
GOOD = {"is_valuable": True, "is_noise": False, "risk": "low", "confidence": 0.9,
        "domain": "quant", "fact_type": "iron_rule", "summary": "s", "reasoning": "r"}


class TestFallbackChain(unittest.TestCase):
    def test_config_parses_chain_and_timeout(self):
        with mock.patch.dict("os.environ", ENV, clear=False):
            cfg = gov.router_config()
        self.assertEqual(cfg["primary_model"], "m-primary")
        self.assertEqual(cfg["fallback_models"], ["m-fb1", "m-fb2"])
        self.assertEqual(cfg["timeout_s"], 45)

    def test_legacy_single_fallback_still_works(self):
        env = {k: v for k, v in ENV.items() if k != "MIMIR_GOVERNANCE_FALLBACK_MODELS"}
        env["MIMIR_GOVERNANCE_FALLBACK_MODEL"] = "m-old"
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = gov.router_config()
        self.assertEqual(cfg["fallback_models"], ["m-old"])

    def test_walks_chain_until_success(self):
        calls = []

        def fake(prompt, model):
            calls.append(model)
            return (GOOD, None) if model == "m-fb2" else (None, "HTTPError 429")

        with mock.patch.dict("os.environ", ENV, clear=False), \
                mock.patch.object(gov, "_call_llm", side_effect=fake):
            res = gov.assess_candidate("用户要求回测必须用真实 PIT 数据，禁止前视偏差。", "c1")
        self.assertEqual(calls, ["m-primary", "m-fb1", "m-fb2"])
        self.assertTrue(res.success)
        self.assertEqual(res.model_used, "m-fb2")

    def test_all_fail_reports_every_cause(self):
        with mock.patch.dict("os.environ", ENV, clear=False), \
                mock.patch.object(gov, "_call_llm", return_value=(None, "HTTPError 429")):
            res = gov.assess_candidate("用户要求回测必须用真实 PIT 数据，禁止前视偏差。", "c2")
        self.assertFalse(res.success)
        self.assertIn("m-fb1", res.error)
        self.assertIn("m-fb2", res.error)


if __name__ == "__main__":
    unittest.main()
