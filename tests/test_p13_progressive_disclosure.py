# -*- coding: utf-8 -*-
"""roadmap #13：三段式渐进披露（索引 -> 上下文 -> 按 ID 展开全文）。

issue #7 契约：
1. **索引**（~50-100 token/条）：只给 id/summary/分数理由，不给全文
2. **时间线/上下文**：中间段（本卡以索引+游标推进到展开）
3. **仅对选中 ID 展开全文**（~500-1000 token/条）

硬约束：
- 游标**服务端持有不下发**（见 recall_cursor）
- 跨步拒绝：任意 ID / 改范围 / 过期 token
- **默认 detail=full，逐字节不变**（零破坏）——绝不在 #13 里犯 #14 的忌
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.recall_cursor import (
    CursorScopeMismatch,
    CursorSignatureInvalid,
    mint_cursor,
    verify_cursor,
)
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _kernel_and_ids(n=3, body="lorem ipsum dolor sit amet " * 40):
    tmp = Path(tempfile.mkdtemp())
    store = CanonicalStore(tmp / "canonical.db")
    fts = FTSProjector(tmp / "fts.db")
    ids = []
    for i in range(n):
        ids.append(store.create_fact(CreateFact(
            content=f"alpha beta gamma item{i} {body}",
            summary=f"index summary item{i}",
            owner_principal="mentor", domain="knowledge",
            fact_type="pattern",   # L2，standard 档可见
            visibility="all", sensitivity="internal", egress_policy="local_only",
            confidence_score=0.9, human_status="confirmed",
        ), actor_principal="mentor")["fact_id"])
    ProjectorRunner(store, fts).run_once(limit=100)
    return QueryKernel(store, fts=fts), ids


class TestDetailDefaultIsFull(unittest.TestCase):
    """零破坏：不传 detail 时行为必须与从前一致（带全文）。"""

    def test_default_returns_full_content(self):
        kernel, _ = _kernel_and_ids()
        out = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor"))
        self.assertTrue(out["results"])
        first = out["results"][0]
        self.assertIn("content", first)
        self.assertGreater(len(first["content"]), 200,
                           "默认档必须返回全文（不得被索引档偷偷替换）")
        self.assertNotIn("recall_cursor", out)

    def test_explicit_full_matches_default(self):
        kernel, _ = _kernel_and_ids()
        a = kernel.search(QueryRequest(text="alpha beta gamma",
                                       principal_id="mentor"))
        b = kernel.search(QueryRequest(text="alpha beta gamma",
                                       principal_id="mentor",
                                       detail="full"))
        self.assertEqual(len(a["results"]), len(b["results"]))
        self.assertEqual(a["results"][0]["content"], b["results"][0]["content"])


class TestIndexStage(unittest.TestCase):
    """detail=index：只给索引段，不下发全文，并下发游标。"""

    def test_index_omits_full_content(self):
        kernel, _ = _kernel_and_ids()
        out = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor",
                                         detail="index"))
        self.assertTrue(out["results"])
        first = out["results"][0]
        self.assertNotIn("content", first,
                         "索引段不得下发全文——那是'换载荷'的形态")
        self.assertIn("summary", first)
        self.assertIn("score_explanation", first)

    def test_index_carries_cursor(self):
        kernel, _ = _kernel_and_ids()
        out = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor",
                                         detail="index"))
        self.assertIn("recall_cursor", out)
        self.assertIsInstance(out["recall_cursor"], str)

    def test_index_cursor_is_opaque(self):
        kernel, ids = _kernel_and_ids()
        out = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor",
                                         detail="index"))
        for fid in ids:
            self.assertNotIn(fid, out["recall_cursor"],
                             "游标不得含内部 fact_id 明文")

    def test_index_is_substantially_smaller(self):
        """索引段载荷应显著小于全文段（这是三段式的全部意义）。"""
        kernel, _ = _kernel_and_ids()
        import json
        full = kernel.search(QueryRequest(text="alpha beta gamma",
                                          principal_id="mentor"))
        idx = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor",
                                         detail="index"))
        full_len = len(json.dumps(full["results"], ensure_ascii=False))
        idx_len = len(json.dumps(idx["results"], ensure_ascii=False))
        self.assertLess(idx_len, full_len * 0.5,
                        f"索引段应显著更小: idx={idx_len} full={full_len}")


class TestExpandStage(unittest.TestCase):
    """按游标 + 选中 ID 展开全文；拒绝任意 ID / 改范围 / 过期。"""

    def test_expand_returns_full_content_for_selected(self):
        kernel, ids = _kernel_and_ids()
        out = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor",
                                         detail="index"))
        expanded = kernel.expand(out["recall_cursor"], [ids[0]])
        self.assertEqual(len(expanded["results"]), 1)
        self.assertEqual(expanded["results"][0]["fact_id"], ids[0])
        self.assertGreater(len(expanded["results"][0]["content"]), 200)

    def test_expand_rejects_arbitrary_id(self):
        """拒绝任意 ID：未在本次召回发放过的 ID 不得展开。"""
        from mimir_v8.recall_cursor import CursorError
        kernel, ids = _kernel_and_ids()
        out = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor",
                                         detail="index"))
        with self.assertRaises(CursorError):
            kernel.expand(out["recall_cursor"], ["not-an-issued-id"])

    def test_expand_rejects_scope_mismatch(self):
        kernel, ids = _kernel_and_ids()
        req = QueryRequest(text="alpha beta gamma", principal_id="mentor",
                           detail="index")
        out = kernel.search(req)
        # 拿 A 查询游标去续另一个查询范围（声明错误的 expected_scope）
        wrong = kernel.cursor_scope(
            QueryRequest(text="完全不同的问题", principal_id="mentor"))
        with self.assertRaises(CursorScopeMismatch):
            kernel.expand(out["recall_cursor"], [ids[0]], expected_scope=wrong)

    def test_expand_rejects_tampered_cursor(self):
        kernel, ids = _kernel_and_ids()
        out = kernel.search(QueryRequest(text="alpha beta gamma",
                                         principal_id="mentor",
                                         detail="index"))
        bad = out["recall_cursor"][:-4] + "zzzz"
        with self.assertRaises(CursorSignatureInvalid):
            kernel.expand(bad, [ids[0]])


class TestInvalidDetail(unittest.TestCase):
    def test_unknown_detail_is_rejected(self):
        kernel, _ = _kernel_and_ids()
        with self.assertRaises(ValueError):
            kernel.search(QueryRequest(text="alpha beta gamma",
                                       principal_id="mentor",
                                       detail="bogus"))


class TestHttpEndpoints(unittest.TestCase):
    """#13 必须**经 HTTP 可用**——否则就是"建了没接线"。"""

    def setUp(self):
        import hashlib
        import json
        from fastapi.testclient import TestClient
        from mimir_v8.api import ServiceContext, create_app
        from mimir_v8.auth import TokenStore
        from mimir_v8.knowledge import FeedbackLoop, KnowledgeService, UnifiedSearch
        from mimir_v8.projector import FTSProjector, ProjectorRunner
        from mimir_v8.schema import CreateFact
        from mimir_v8.store import CanonicalStore

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        store = CanonicalStore(root / "canonical.db")
        fts = FTSProjector(root / "fts.db")
        self.kernel = QueryKernel(store, fts=fts)
        self.ids = []
        for i in range(3):
            self.ids.append(store.create_fact(CreateFact(
                content=f"alpha beta gamma item{i} " + ("lorem ipsum " * 40),
                summary=f"idx summary {i}", owner_principal="mentor",
                domain="knowledge", fact_type="pattern",
                visibility="all", sensitivity="internal",
                egress_policy="local_only",
                confidence_score=0.9, human_status="confirmed",
            ), actor_principal="mentor")["fact_id"])
        ProjectorRunner(store, fts).run_once(limit=100)

        knowledge = KnowledgeService(store)
        unified = UnifiedSearch(self.kernel, knowledge,
                                enabled_layers=("memory", "learning"))
        feedback = FeedbackLoop(store, knowledge)
        self.token = "p13-http-mentor"
        token_path = root / "tokens.json"
        token_path.write_text(json.dumps({"principals": [{
            "id": "mentor",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            "scopes": ["read", "write", "ingest"],
            "roles": [], "admin": False,
        }]}), encoding="utf-8")
        ctx = ServiceContext(store=store, token_store=TokenStore(token_path),
                             query=self.kernel, knowledge=knowledge,
                             unified_search=unified, feedback_loop=feedback)
        self.client = TestClient(create_app(ctx), raise_server_exceptions=False)

    def tearDown(self):
        self._tmp.cleanup()

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_query_detail_index_over_http(self):
        r = self.client.post("/v8/query",
                             json={"text": "alpha beta gamma", "limit": 5,
                                   "detail": "index"},
                             headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("recall_cursor", body)
        self.assertNotIn("content", body["results"][0])

    def test_query_default_detail_stays_full_over_http(self):
        r = self.client.post("/v8/query",
                             json={"text": "alpha beta gamma", "limit": 5},
                             headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertNotIn("recall_cursor", body)
        self.assertIn("content", body["results"][0])

    def test_expand_endpoint_returns_full_text(self):
        r = self.client.post("/v8/query",
                             json={"text": "alpha beta gamma", "limit": 5,
                                   "detail": "index"},
                             headers=self._h())
        cursor = r.json()["recall_cursor"]
        r2 = self.client.post("/v8/query/expand",
                              json={"cursor": cursor,
                                    "fact_ids": [self.ids[0]]},
                              headers=self._h())
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["results"][0]["fact_id"], self.ids[0])
        self.assertGreater(len(r2.json()["results"][0]["content"]), 200)

    def test_expand_endpoint_rejects_arbitrary_id(self):
        r = self.client.post("/v8/query",
                             json={"text": "alpha beta gamma", "limit": 5,
                                   "detail": "index"},
                             headers=self._h())
        cursor = r.json()["recall_cursor"]
        r2 = self.client.post("/v8/query/expand",
                              json={"cursor": cursor,
                                    "fact_ids": ["never-issued-id"]},
                              headers=self._h())
        self.assertGreaterEqual(r2.status_code, 400,
                                "任意 ID 必须被拒，不得 200")


if __name__ == "__main__":
    unittest.main()
