# -*- coding: utf-8 -*-
"""1.0-C2/C3 原文证据召回 + 出口包裹（「出口诚实」组）。

C2：检索 per-result 带 evidence 引文（quote_text/source_name/message_ts，
top-2 配额）；候选与记忆原文重合度>0.6 打 verbatim_overlap 标——消费方
注入 prompt 时「有据可查」。
C3：检索响应顶层带 injection_safe_wrap——官方「数据非指令」包裹模板，
dashboard/插件统一采用，与 P0-D 的治理腿（delimited 块）拼成完整注入面。

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


def _kernel(store):
    return QueryKernel(store, vector=None, fts=None, graph=None)


def _seed(store, content, *, fact_type="iron_rule", owner="mentor"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal=owner,
        fact_type=fact_type, domain="knowledge", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)["fact_id"]


class TestEvidenceRecall(unittest.TestCase):
    """C2: per-result evidence 引文。"""

    def test_result_carries_evidence_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            _seed(store, "生产库只允许经 API 写入，禁止直连")
            k = _kernel(store)
            r = k.search(QueryRequest(text="生产库 写入", principal_id="mentor",
                                       limit=5, use_anchor=True))
            self.assertTrue(r["results"])
            top = r["results"][0]
            self.assertIn("evidence", top)
            # evidence 是列表形态（即便为空也键在——契约稳定）
            self.assertIsInstance(top["evidence"], list)

    def test_verbatim_overlap_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            _seed(store, "生产库只允许经 API 写入，禁止直连")
            k = _kernel(store)
            r = k.search(QueryRequest(text="生产库只允许经 API 写入",
                                      principal_id="mentor", limit=5,
                                      use_anchor=True))
            top = r["results"][0]
            # 查询词与原文高度重合 → verbatim_overlap=True（有据可查信号）
            self.assertTrue(top.get("verbatim_overlap"))


class TestInjectionSafeWrap(unittest.TestCase):
    """C3: 响应顶层 injection_safe_wrap 官方模板。"""

    def test_wrap_present_and_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            _seed(store, "生产库只允许经 API 写入")
            k = _kernel(store)
            r = k.search(QueryRequest(text="生产库", principal_id="mentor",
                                       limit=5, use_anchor=True))
            self.assertIn("injection_safe_wrap", r)
            wrap = r["injection_safe_wrap"]
            self.assertIn("data", wrap.lower())
            self.assertIn("instruction", wrap.lower())
            # 模板必须可复制即用（字符串形态非结构体）
            self.assertIsInstance(wrap, str)

    def test_trace_also_wrapped(self):
        """trace 与 search 两口径不分叉（P46 判例延续）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            _seed(store, "生产库只允许经 API 写入")
            k = _kernel(store)
            r = k.trace(QueryRequest(text="生产库", principal_id="mentor",
                                     limit=5, use_anchor=True))
            self.assertIn("injection_safe_wrap", r)


if __name__ == "__main__":
    unittest.main()
