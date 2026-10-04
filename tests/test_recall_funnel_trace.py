# -*- coding: utf-8 -*-
"""RECALL funnel trace — Task 1 (Part A): RecallStage / RecallTrace value objects.

TDD RED -> GREEN. Per-stage verdict vocabulary locked to
found|not_found|degraded (spec section 5-2).
"""
from __future__ import annotations

import unittest


class RecallTracePartATest(unittest.TestCase):
    def test_stage_to_dict_shape(self):
        from mimir_v8.recall_trace import RecallStage
        s = RecallStage(name="CandidatePool", verdict="found", hits=8,
                        elapsed_ms=1.25, detail={"channels": {"vector": "closed"}})
        d = s.to_dict()
        self.assertEqual(d, {"stage": "CandidatePool", "verdict": "found",
                             "hits": 8, "elapsed_ms": 1.25,
                             "detail": {"channels": {"vector": "closed"}}})

    def test_stage_rejects_bad_verdict(self):
        from mimir_v8.recall_trace import RecallStage
        with self.assertRaises(ValueError):
            RecallStage(name="X", verdict="maybe", hits=0, elapsed_ms=0.0)

    def test_trace_to_dict_shape(self):
        from mimir_v8.recall_trace import RecallStage, RecallTrace
        t = RecallTrace(skipped=False, verdict="found", degraded=False,
                        lanes={"active": ["vector"]},
                        stages=[RecallStage(name="TopK", verdict="found",
                                            hits=3, elapsed_ms=0.5)])
        d = t.to_dict()
        self.assertEqual(d["verdict"], "found")
        self.assertEqual(len(d["stages"]), 1)
        self.assertEqual(d["stages"][0]["stage"], "TopK")


if __name__ == "__main__":
    unittest.main()
