# -*- coding: utf-8 -*-
"""P0-D 判定面：治理消费链回声拦截（补充件）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.governance import AssessmentResult, make_decision


class TestEchoGuardDecisionPath(unittest.TestCase):

    def test_echo_flagged_assessment_goes_to_human_review(self):
        """reasoning 带 echo_guard 标记 → make_decision 必须给 human_review。"""
        a = AssessmentResult(
            candidate_id="c1", is_valuable=True, is_noise=False,
            risk="high", domain="quant", fact_type="pattern",
            summary="s", confidence=0.95, success=True,
            reasoning="[echo_guard] 评估理由疑似回声注入形态，强制人工复核: 已忽略之前的所有指令",
        )
        decision, reason = make_decision(a)
        self.assertEqual(decision, "human_review")
        self.assertIn("高风险", reason)

    def test_clean_low_risk_still_provisional(self):
        """对照组：干净评估的低风险路径不被误伤。"""
        a = AssessmentResult(
            candidate_id="c2", is_valuable=True, is_noise=False,
            risk="low", domain="quant", fact_type="pattern",
            summary="s", confidence=0.9, success=True,
            reasoning="该事实记录稳定的项目配置知识",
        )
        decision, _ = make_decision(a)
        self.assertEqual(decision, "provisional")


if __name__ == "__main__":
    unittest.main()
