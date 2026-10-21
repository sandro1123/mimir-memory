# -*- coding: utf-8 -*-
"""issue #13：积压清仓的**逐条处理**面 —— 人工必须先看得到全文才能拍板。

实测（2026-10-21，生产）：
- 12 条 human_review 积压，**12/12 都成功评估过**（risk low×5 / medium×5 /
  high×2），全部是真实内容，**0/12 已在 canonical 中**。
- 结论：这不是"系统坏了"，是**治理按设计送进人工队列、但没人处理**。
- 由此推翻"批量 reject/approve"的方向：批量拒掉 = 永久丢失 12 条真事实。

正确做法是**逐条可处理**，且拍板前必须看得到**全文**（列表只给 200 字截断）。

TDD RED：`get_pending_item` 尚不存在。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.candidates import CandidateService, CreateCandidate
from mimir_v8.review import ReviewQueue
from mimir_v8.store import CanonicalStore

LONG_BODY = (
    "这是一条很长的待审事实正文，用于验证列表截断与详情全文的差异。" * 8
    + " 结尾标记：TAIL-MARKER-9f3a"
)


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


class TestPendingItemDetail(_Base):
    def test_detail_returns_full_content(self):
        """详情必须给全文——列表是 200 字截断，拍板看截断等于闭眼签。"""
        c = self._cand(LONG_BODY, "long1")
        self._set_status(c["candidate_id"], "human_review")

        detail = self.queue.get_pending_item(c["candidate_id"])
        self.assertEqual(detail["content"], LONG_BODY)
        self.assertIn("TAIL-MARKER-9f3a", detail["content"],
                      "全文必须含尾部标记（列表截断会丢掉它）")

        listed = self.queue.list_pending()[0]
        self.assertNotIn("TAIL-MARKER-9f3a", listed.content,
                         "前置：列表确实截断，否则本测试证明不了什么")

    def test_detail_carries_review_context(self):
        """详情要带拍板所需上下文：状态、风险、等待时长、不确定原因。"""
        c = self._cand("一条需要人工判断的事实：跨部门数据授权范围待定", "ctx1")
        self._set_status(c["candidate_id"], "human_review")

        d = self.queue.get_pending_item(c["candidate_id"])
        for key in ("candidate_id", "status", "risk", "wait_hours",
                    "proposed_owner", "uncertainty_json"):
            self.assertIn(key, d, f"详情缺少拍板必需字段：{key}")
        self.assertEqual(d["status"], "human_review")
        self.assertIn(d["risk"], {"low", "medium", "high", "critical"})

    def test_detail_rejects_terminal_status(self):
        """终态不属待审——不得经此口子被当成"待处理"重新捞出来。"""
        c = self._cand("已经提交过的事实：常规配置", "term1")
        self._set_status(c["candidate_id"], "committed")
        self.assertIsNone(self.queue.get_pending_item(c["candidate_id"]))

    def test_detail_unknown_id_returns_none(self):
        self.assertIsNone(self.queue.get_pending_item("no-such-candidate"))

    def test_detail_does_not_mutate(self):
        """只读：取详情不得改变任何状态（防"看一眼就动数据"）。"""
        c = self._cand("只读验证用的事实：机房温度策略", "ro1")
        self._set_status(c["candidate_id"], "human_review")
        self.queue.get_pending_item(c["candidate_id"])
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT status, updated_at FROM candidate_facts WHERE candidate_id=?",
                (c["candidate_id"],)).fetchone()
        self.assertEqual(row["status"], "human_review")


if __name__ == "__main__":
    unittest.main()
