# -*- coding: utf-8 -*-
"""issue #13：pending 老化分级 —— 防止"12 条积压 13 天无人发现"重演。

实测背景（2026-10-22）：
- 生产 `mimir-v9.2-review-reminder.timer` 每 2 小时跑一次，**每次输出
  `total: 0`**，而库里真有 12 条 human_review 积压 —— 因为**部署版仍是
  1.3.0**（`releases/current -> releases/1.3.0-20261009_102805`），其
  `review.py` 只查 `review_required`。
- 即使部署了修复，`review_reminder` 也只给一个 `oldest_wait_hours` 数字，
  没有"哪些已经老到该管"的判定 —— 13 天和 13 小时在输出里几乎没区别。

本卡补：老化分级（fresh / aging / stale），让告警能**自己说出严重程度**，
而不是等人去看那个数字。

TDD RED：`age_buckets` 等符号尚不存在。
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


class _Base(unittest.TestCase):
    owner = "mentor"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canon.db")
        self.svc = CandidateService(self.store)
        self.queue = ReviewQueue(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _cand(self, content, key, days_old=0):
        r = self.svc.create_candidate(CreateCandidate(
            content=content, summary=content[:40],
            proposed_owner_principal=self.owner,
            proposed_domain="knowledge", proposed_fact_type="reference",
            source_id=None, source_hash=None, idempotency_key=f"cand-{key}",
        ), actor_principal=self.owner)
        cid = r["candidate_id"]
        if days_old:
            import datetime
            old = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.timedelta(days=days_old)).isoformat()
            with self.store.transaction() as conn:
                conn.execute("UPDATE candidate_facts SET status='human_review', "
                             "created_at=? WHERE candidate_id=?", (old, cid))
        return cid


class TestAgeBuckets(unittest.TestCase):
    def test_bucket_boundaries(self):
        """分级边界必须明确：13 小时≠13 天，不能只给一个数字。"""
        from mimir_v8.review import age_bucket

        self.assertEqual(age_bucket(0.5), "fresh")
        self.assertEqual(age_bucket(23.9), "fresh")
        self.assertEqual(age_bucket(24.0), "aging")
        self.assertEqual(age_bucket(71.9), "aging")
        self.assertEqual(age_bucket(72.0), "stale")
        self.assertEqual(age_bucket(13 * 24), "stale")

    def test_bucket_rejects_negative(self):
        from mimir_v8.review import age_bucket

        with self.assertRaises(ValueError):
            age_bucket(-1.0)


class TestBacklogAging(_Base):
    def test_old_items_are_flagged_stale(self):
        """13 天那条必须被判为 stale——这正是没人发现的那 12 条的形状。"""
        self._cand("十三天前的事实：机房迁移方案待定", "old13", days_old=13)
        self._cand("刚进来的事实：今日配置调整", "new0", days_old=0)

        report = self.queue.backlog_aging()
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["by_bucket"]["stale"], 1)
        self.assertEqual(report["by_bucket"]["fresh"], 1)
        self.assertGreaterEqual(report["oldest_days"], 12.9)

    def test_stale_ids_are_listed(self):
        """要能直接拿到"该管的那些"的 ID，而不只是计数。"""
        cid = self._cand("四天前的事实：跨部门授权待定", "old4", days_old=4)
        report = self.queue.backlog_aging()
        self.assertIn(cid, report["stale_candidate_ids"])

    def test_empty_queue_is_calm(self):
        """空队列不得制造告警噪声。"""
        report = self.queue.backlog_aging()
        self.assertEqual(report["total"], 0)
        self.assertFalse(report["needs_attention"])
        self.assertEqual(report["stale_candidate_ids"], [])

    def test_needs_attention_only_on_stale(self):
        """只有出现 stale 才喊——aging 不算事，否则又是噪声。"""
        self._cand("一天半的事实：待定事项", "age36", days_old=1.5)
        report = self.queue.backlog_aging()
        self.assertEqual(report["by_bucket"]["aging"], 1)
        self.assertFalse(report["needs_attention"],
                         "aging 不该触发告警，否则天天响")

        self._cand("五天的事实：又一条待定", "old5", days_old=5)
        report2 = self.queue.backlog_aging()
        self.assertTrue(report2["needs_attention"])

    def test_counts_do_not_double_count(self):
        """分桶之和必须等于 total（防重复计数把积压说大）。"""
        self._cand("事实 A：一天", "d1", days_old=1)
        self._cand("事实 B：四天", "d4", days_old=4)
        self._cand("事实 C：十天", "d10", days_old=10)
        report = self.queue.backlog_aging()
        self.assertEqual(sum(report["by_bucket"].values()), report["total"])
        self.assertEqual(report["total"], 3)


class TestReminderWiring(_Base):
    """级数必须出现在**真实告警输出**里，否则等于没做（建了没接线）。"""

    def test_review_reminder_surfaces_aging(self):
        from mimir_v8.worker import review_reminder

        self._cand("十天前的事实：该管却没人管", "wire10", days_old=10)
        out = review_reminder(self.store)

        self.assertIn("aging", out, "告警输出必须带老化分级")
        self.assertEqual(out["aging"]["by_bucket"]["stale"], 1)
        self.assertTrue(out["needs_attention"],
                        "有 stale 时告警必须明确说需要关注")
        self.assertEqual(out["items"][0]["age_bucket"], "stale")

    def test_summary_zero_but_aging_nonzero_would_be_a_bug(self):
        """回归钉子：total 与 aging.total 必须同源。

        生产事故的形状正是 `total: 0` 而库里有 12 条 —— 两者口径一旦
        分叉就会出现这种自相矛盾的告警。
        """
        from mimir_v8.worker import review_reminder

        self._cand("一条待审事实", "wire1", days_old=5)
        out = review_reminder(self.store)
        self.assertEqual(out["total"], out["aging"]["total"],
                         "total 与 aging.total 必须一致（同一口径）")


if __name__ == "__main__":
    unittest.main()
