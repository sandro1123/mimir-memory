# -*- coding: utf-8 -*-
"""roadmap #16 Tier-2 / issue #13：pending 治理面 —— human_review 必须可见。

实测病根（2026-10-21，生产 808 条 active facts）：
- `review_required` 队列 **0 条**，`human_review` 状态 **12 条积压**
  （最长 13 天），全为 `automatic_extraction_requires_review`。
- `review.py` 的 `ReviewQueue.list_pending` **只查 `status='review_required'`**，
  于是 `human_review` 人工**看不见**。
- `worker.py:review_requeue` 的 docstring 宣称"now consumable via the review
  endpoint"——**该说法不成立**，正是本条要修的缺口。

TDD RED：当前 `list_pending` 不含 human_review。
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.candidates import CandidateService, CreateCandidate
from mimir_v8.review import ReviewQueue
from mimir_v8.store import CanonicalStore


class _Base(unittest.TestCase):
    owner = "mentor"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canon.db")
        self.svc = CandidateService(self.store)
        self.queue = ReviewQueue(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _cand(self, content, key):
        return self.svc.create_candidate(CreateCandidate(
            content=content, summary=content[:40],
            proposed_owner_principal=self.owner,
            proposed_domain="knowledge", proposed_fact_type="reference",
            source_id=None, source_hash=None, idempotency_key=f"cand-{key}",
        ), actor_principal=self.owner)

    def _set_status(self, candidate_id, status):
        with self.store.transaction() as conn:
            conn.execute("UPDATE candidate_facts SET status=? WHERE candidate_id=?",
                         (status, candidate_id))


class TestHumanReviewVisibility(_Base):
    def test_human_review_candidate_appears_in_queue(self):
        """核心缺口：human_review 必须出现在待审队列里。"""
        c = self._cand("一条需要人工判断的事实：服务器机房迁移方案待定", "hr1")
        self._set_status(c["candidate_id"], "human_review")

        items = self.queue.list_pending()
        ids = [i.candidate_id for i in items]
        self.assertIn(c["candidate_id"], ids,
                      "human_review 候选必须对人工可见（此前 12 条积压全部隐形）")

    def test_backlog_is_counted_in_summary(self):
        """summarize 的总数必须含 human_review，否则看板上永远显示 0 积压。"""
        c = self._cand("另一条待人工判断的事实：采购预算超限需复核", "hr2")
        self._set_status(c["candidate_id"], "human_review")
        summary = self.queue.summarize()
        self.assertEqual(summary.total, 1, "summarize 必须计入 human_review 积压")

    def test_review_required_still_visible(self):
        """零破坏：原有 review_required 路径不变。"""
        c = self._cand("普通待审事实：日志轮转策略调整", "rr1")
        items = self.queue.list_pending()
        self.assertIn(c["candidate_id"], [i.candidate_id for i in items])

    def test_status_is_exposed_per_item(self):
        """每条必须带自己的状态——否则人工分不清"待评估"与"要我拍板"。"""
        a = self._cand("待评估事实 A：磁盘配额策略", "st1")
        b = self._cand("待人工事实 B：跨部门数据授权", "st2")
        self._set_status(b["candidate_id"], "human_review")

        items = {i.candidate_id: i for i in self.queue.list_pending()}
        self.assertEqual(items[a["candidate_id"]].status, "review_required")
        self.assertEqual(items[b["candidate_id"]].status, "human_review")

    def test_other_statuses_excluded(self):
        """committed/rejected 等终态不得混入待审队列（防噪声）。"""
        c = self._cand("已提交事实：常规配置项", "ex1")
        self._set_status(c["candidate_id"], "committed")
        self.assertEqual(self.queue.list_pending(), [])

    def test_provisional_excluded(self):
        """provisional 是快车道中间态，不是人工待办。"""
        c = self._cand("临时事实：缓存预热", "pv1")
        self._set_status(c["candidate_id"], "provisional")
        self.assertEqual(self.queue.list_pending(), [])


if __name__ == "__main__":
    unittest.main()
