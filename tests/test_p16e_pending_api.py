# -*- coding: utf-8 -*-
"""issue #13：待审队列 HTTP 端点 —— 让 12 条积压真的"看得见、点得开"。

前情：库层已修（list_pending 含 human_review，8d599a8），但若没有端点，
人工仍然碰不到——**修在库里没人调用 = 又回到"建了没接线"**。

本测试钉三件事：
1. 端点存在且返回 human_review 积压
2. 详情端点返回**全文**（列表是 200 字截断）
3. owner 边界：不能看别人的候选（ACL 不得因新端点开洞）
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.candidates import CandidateService, CreateCandidate
from mimir_v8.query import QueryKernel
from mimir_v8.store import CanonicalStore

LONG_BODY = (
    "这是一条很长的待审事实正文，用于验证端点返回全文而非截断。" * 8
    + " 结尾标记：TAIL-MARKER-9f3a"
)


class _Fixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        self.svc = CandidateService(self.store)
        plain_tokens = {
            "mentor": "p16d-mentor-token",
            "quantmaster": "p16d-quantmaster-token",
            "admin": "p16d-admin-token",
        }
        scopes = ["read", "write", "ingest", "review"]
        token_path = root / "tokens.json"
        token_path.write_text(json.dumps({
            "principals": [
                {
                    "id": principal,
                    "token_sha256": hashlib.sha256(
                        token.encode("utf-8")).hexdigest(),
                    "scopes": scopes,
                    "roles": ["admin"] if principal == "admin" else [],
                    "admin": principal == "admin",
                }
                for principal, token in plain_tokens.items()
            ]
        }), encoding="utf-8")
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(token_path),
            query=self.query,
        )
        self.client = TestClient(create_app(context), raise_server_exceptions=False)
        self.tokens = plain_tokens

    def headers(self, principal="mentor"):
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    def cand(self, content, key, owner="mentor"):
        return self.svc.create_candidate(CreateCandidate(
            content=content, summary=content[:40],
            proposed_owner_principal=owner,
            proposed_domain="knowledge", proposed_fact_type="reference",
            source_id=None, source_hash=None, idempotency_key=f"cand-{key}",
        ), actor_principal=owner)

    def set_status(self, candidate_id, status):
        with self.store.transaction() as conn:
            conn.execute("UPDATE candidate_facts SET status=? WHERE candidate_id=?",
                         (status, candidate_id))


class TestPendingQueueAPI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = _Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_queue_lists_human_review_backlog(self):
        c = self.fx.cand("待人工判断的事实：跨部门数据授权范围", "api1")
        self.fx.set_status(c["candidate_id"], "human_review")

        r = self.fx.client.get("/v8/learning/candidates/pending",
                               headers=self.fx.headers())
        self.assertEqual(r.status_code, 200, r.text)
        ids = [i["candidate_id"] for i in r.json()["items"]]
        self.assertIn(c["candidate_id"], ids,
                      "端点必须能列出 human_review 积压——否则 12 条依然隐形")

    def test_detail_endpoint_returns_full_content(self):
        c = self.fx.cand(LONG_BODY, "api2")
        self.fx.set_status(c["candidate_id"], "human_review")

        r = self.fx.client.get(
            f"/v8/learning/candidates/{c['candidate_id']}/pending",
            headers=self.fx.headers())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("TAIL-MARKER-9f3a", r.json()["content"],
                      "详情端点必须给全文，截断的正文不能用来拍板")

    def test_detail_unknown_returns_404(self):
        r = self.fx.client.get("/v8/learning/candidates/nope/pending",
                               headers=self.fx.headers())
        self.assertEqual(r.status_code, 404, r.text)

    def test_owner_boundary_enforced(self):
        """新人看不到别人的候选（ACL 不得因新端点开洞）。"""
        c = self.fx.cand("mentor 的私有候选：机房迁移方案", "api3", owner="quantmaster")
        self.fx.set_status(c["candidate_id"], "human_review")

        r = self.fx.client.get(
            f"/v8/learning/candidates/{c['candidate_id']}/pending",
            headers=self.fx.headers("mentor"))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["error"]["code"], "owner_boundary")

    def test_requires_review_scope(self):
        r = self.fx.client.get("/v8/learning/candidates/pending")
        self.assertEqual(r.status_code, 401, r.text)


if __name__ == "__main__":
    unittest.main()
