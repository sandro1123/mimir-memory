# -*- coding: utf-8 -*-
"""P0-A 召回诚实判语：search/trace 顶层 recall_verdict 一词定论。

v14.2 P0（09-08 用户拍板 GO）。与「空≠错」纪律同源：degraded（故障
先于缺失）必须与 not_found（正常但零命中）语义分家，让 dashboard
健康灯与「问我的助手」直接消费，不再需要拼装 channels/布尔。

三态：
- degraded：P46 顶层 degraded 为真（任一相似度通道 degraded/open）
- not_found：正常检索，零命中或全被滤
- found：其余（有结果）

TDD RED → GREEN.
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


def _kernel(store, vector=None, fts=None):
    return QueryKernel(store, vector=vector, fts=fts, graph=None,
                       embedder=None if vector is None else (lambda q: [0.0]))


def _seed(store, content, *, fact_type="iron_rule", owner="mentor"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal=owner,
        fact_type=fact_type, domain="knowledge", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)["fact_id"]


class TestRecallVerdictSearch(unittest.TestCase):
    """search() 顶层 recall_verdict 三态。"""

    def test_found_when_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "Mímir 是事件溯源的联邦记忆系统")
            kernel = _kernel(store)
            result = kernel.search(QueryRequest(
                text="事件溯源", principal_id="mentor", limit=5, use_anchor=True,
            ))
            self.assertTrue(len(result["results"]) > 0)
            self.assertEqual(result["recall_verdict"], "found")

    def test_not_found_when_zero_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "完全无关的一条事实")
            kernel = _kernel(store)
            # 锚通道是相似度无关的安全底——铁律永远注入（by design）；
            # not_found 判定走普通检索路径（无锚），零命中即诚实 not_found。
            result = kernel.search(QueryRequest(
                text="量子引力波探测", principal_id="mentor", limit=5, use_anchor=False,
            ))
            self.assertEqual(len(result["results"]), 0)
            self.assertEqual(result["recall_verdict"], "not_found")

    def test_degraded_beats_not_found(self):
        """通道故障时即便零命中也是 degraded——故障先于缺失。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "完全无关的一条事实")
            flaky = _FlakyVector()
            flaky.fail = True
            kernel = _kernel(store, vector=flaky)
            result = kernel.search(QueryRequest(
                text="量子引力波探测", principal_id="mentor", limit=5, use_anchor=True,
            ))
            self.assertEqual(result["recall_verdict"], "degraded")
            self.assertTrue(result["degraded"])

    def test_degraded_with_hits_still_degraded(self):
        """降级但有部分结果：仍是 degraded（诚实优先于乐观）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "Mímir 是事件溯源的联邦记忆系统")
            flaky = _FlakyVector()
            flaky.fail = True
            kernel = _kernel(store, vector=flaky)
            result = kernel.search(QueryRequest(
                text="事件溯源", principal_id="mentor", limit=5, use_anchor=True,
            ))
            self.assertEqual(result["recall_verdict"], "degraded")


class TestRecallVerdictTrace(unittest.TestCase):
    """trace() 与 search() 两口径不分叉（P46 判例延续）。"""

    def test_trace_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "Mímir 是事件溯源的联邦记忆系统")
            kernel = _kernel(store)
            result = kernel.trace(QueryRequest(
                text="事件溯源", principal_id="mentor", limit=5, use_anchor=True,
            ))
            self.assertTrue(len(result["results"]) > 0)
            self.assertEqual(result["recall_verdict"], "found")

    def test_trace_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "完全无关的一条事实")
            kernel = _kernel(store)
            result = kernel.trace(QueryRequest(
                text="量子引力波探测", principal_id="mentor", limit=5, use_anchor=False,
            ))
            self.assertEqual(result["recall_verdict"], "not_found")

    def test_trace_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "完全无关的一条事实")
            flaky = _FlakyVector()
            flaky.fail = True
            kernel = _kernel(store, vector=flaky)
            result = kernel.trace(QueryRequest(
                text="量子引力波探测", principal_id="mentor", limit=5, use_anchor=True,
            ))
            self.assertEqual(result["recall_verdict"], "degraded")


class TestRecallVerdictUnified(unittest.TestCase):
    """/v9/search-preview（UnifiedSearch.search）同词。"""

    def test_unified_verdict_present(self):
        from mimir_v8.knowledge import UnifiedSearch, UnifiedSearchRequest
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "Mímir 是事件溯源的联邦记忆系统")
            us = UnifiedSearch.__new__(UnifiedSearch)
            us.rrf_k = 60
            us.enabled_layers = ("memory",)
            us.adapters = {}
            us.memory = type("M", (), {"search": staticmethod(
                lambda req: {"results": []})})()
            result = us.search(UnifiedSearchRequest(
                text="x", principal_id="mentor", limit=5, layers=("memory",),
            ))
            self.assertIn(result["recall_verdict"], ("found", "not_found"))


if __name__ == "__main__":
    unittest.main()
