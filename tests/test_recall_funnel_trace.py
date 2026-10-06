# -*- coding: utf-8 -*-
"""RECALL funnel trace — Task 1 (Part A): RecallStage / RecallTrace value objects.
Task 2 (Part B): search() instrumentation — six stages, trace only when
include_trace=True.

TDD RED -> GREEN. Per-stage verdict vocabulary locked to
found|not_found|degraded (spec section 5-2).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class _FlakyVector:
    """可编程炸/好的假 vector 后端（克隆自 test_p46_resilience_tier）。"""

    def __init__(self):
        self.fail = False
        self.calls = 0

    def query(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("chroma backend down (simulated)")
        return {"ids": [[]], "distances": [[]]}


class _FlakyFTS:
    def __init__(self):
        self.fail = False
        self.calls = 0

    def search_ids(self, query, limit=50):
        self.calls += 1
        if self.fail:
            raise RuntimeError("fts index locked (simulated)")
        return []


def _seed(store, content, *, fact_type, owner="mentor", domain="knowledge"):
    human_status = "unreviewed" if fact_type == "ephemeral" else "confirmed"
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain=domain,
            fact_type=fact_type,
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status=human_status,
        ),
        actor_principal=owner,
    )["fact_id"]


class FunnelTraceFixture:
    """真 store + 锚通道事实 + 可编程炸的 vector/fts 后端。

    ResilienceFixture 的克隆（test_p46_resilience_tier:85-100），
    search() helper 扩展转发 include_trace / text kwargs。
    """

    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.vector = _FlakyVector()
        self.fts = _FlakyFTS()
        self.kernel = QueryKernel(
            self.store, vector=self.vector, fts=self.fts, embedder=lambda text: [0.0] * 8
        )
        self.iron_id = _seed(self.store, "生产库只允许经 API 写入", fact_type="iron_rule")

    def search(self, **kw):
        defaults = dict(text="生产库 API 写入 铁律", principal_id="mentor", limit=20)
        defaults.update(kw)
        return self.kernel.search(QueryRequest(**defaults))


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


class RecallFunnelTracePartBTest(unittest.TestCase):
    """Task 2: search() 六段漏斗埋点 —— trace 仅在 include_trace=True 时附带。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = FunnelTraceFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_search_without_trace_has_no_trace_key(self):
        r = self.fx.search()  # include_trace defaults False
        self.assertNotIn("recall_trace", r)

    def test_search_with_trace_returns_all_stages(self):
        r = self.fx.search(include_trace=True)
        t = r["recall_trace"]
        names = [s["stage"] for s in t["stages"]]
        for want in ("RelevanceGate", "CandidatePool", "AnchorChannel",
                     "LayerSweep", "HydrationFilter", "TopK"):
            self.assertIn(want, names, f"missing stage {want}")
        for s in t["stages"]:
            self.assertIn(s["verdict"], ("found", "not_found", "degraded"))
            self.assertGreaterEqual(s["elapsed_ms"], 0.0)
        self.assertEqual(t["verdict"], r["recall_verdict"],
                         "trace verdict must equal top-level recall_verdict")

    def test_trace_marks_degraded_channel_stage(self):
        self.fx.vector.fail = True
        r = self.fx.search(include_trace=True)
        pool = next(s for s in r["recall_trace"]["stages"]
                    if s["stage"] == "CandidatePool")
        self.assertEqual(pool["verdict"], "degraded")
        self.assertEqual(pool["detail"]["channels"]["vector"], "degraded")
        self.assertEqual(r["recall_trace"]["verdict"], "degraded")

    def test_trace_empty_query_reports_gate_skip(self):
        r = self.fx.search(text="   ", include_trace=True)
        t = r["recall_trace"]
        self.assertTrue(t["skipped"])
        self.assertEqual(t["stages"][0]["stage"], "RelevanceGate")
        self.assertEqual(t["stages"][0]["verdict"], "not_found")


if __name__ == "__main__":
    unittest.main()
