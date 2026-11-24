# -*- coding: utf-8 -*-
"""B8: fast-track 判重闸拦截是设计内去向，不触发 exit 1，且不再反复重试.

2026-11-24 生产实况：DuplicateBlocked 进 errors → 治理 timer 每 15 分钟
exit 1（24h 假失败 66 次），被拦候选留在 provisional 被同一闸反复拦。
修复后：被拦候选转 human_review（人可见、附原因），结果进
duplicate_blocked，errors 为空 → exit 0；真异常仍 exit 1。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8 import worker
from mimir_v8.candidates import DuplicateBlocked
from mimir_v8.governance import fast_track_commit_all
from mimir_v8.store import CanonicalStore


def _insert_provisional(store, cid, confidence=0.99):
    now = "2026-11-24T00:00:00+00:00"
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_facts(candidate_id,content,summary,proposed_owner_principal,"
            "proposed_domain,proposed_fact_type,proposed_visibility,proposed_sensitivity,"
            "proposed_egress_policy,proposed_by,uncertainty_json,confidence_score,"
            "status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, f"content {cid}", f"summary {cid}", "mentor", "system", "pattern",
             "owner_only", "internal", "local_only", "service:test",
             '["possible_duplicate:merge:1.0"]', confidence, "provisional", now, now),
        )


def _status(store, cid):
    with store.transaction() as c:
        row = c.execute(
            "SELECT status, review_reason FROM candidate_facts WHERE candidate_id=?", (cid,)
        ).fetchone()
    return row["status"], row["review_reason"]


class TestFastTrackDuplicateBlocked(unittest.TestCase):
    def _run(self, store, svc):
        import mimir_v8.governance as gov
        with mock.patch.object(gov, "GOVERNANCE_AUTO_APPROVE", True):
            return fast_track_commit_all(store, svc)

    def test_duplicate_blocked_moves_to_human_review_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "ft-dup.db")
            _insert_provisional(store, "c-dup")
            svc = mock.Mock()
            svc.review_candidate.side_effect = DuplicateBlocked(
                "c-dup", {"match_type": "merge", "level": "strong_warn", "matched_fact_id": None})
            result = self._run(store, svc)
            status, reason = _status(store, "c-dup")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["committed"], 0)
        self.assertEqual([d["candidate_id"] for d in result["duplicate_blocked"]], ["c-dup"])
        self.assertEqual(status, "human_review")
        self.assertIn("判重闸拦截", reason)

    def test_blocked_candidate_not_retried_next_round(self):
        """转态后下一轮 fast_track 不再选中它（provisional 队列已空）."""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "ft-dup2.db")
            _insert_provisional(store, "c-dup")
            svc = mock.Mock()
            svc.review_candidate.side_effect = DuplicateBlocked(
                "c-dup", {"match_type": "merge", "level": "strong_warn", "matched_fact_id": None})
            self._run(store, svc)
            svc.review_candidate.reset_mock()
            second = self._run(store, svc)
        svc.review_candidate.assert_not_called()
        self.assertEqual(second.get("committed"), 0)

    def test_worker_exit_code_zero_for_duplicate_only(self):
        self.assertEqual(worker._exit_code({
            "status": "ok", "processed": 0, "llm_failures": 0,
            "fast_track_committed": 1,
            "fast_track_duplicate_blocked": [{"candidate_id": "c"}],
        }), 0)

    def test_real_error_still_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "ft-hard.db")
            _insert_provisional(store, "c-hard")
            svc = mock.Mock()
            svc.review_candidate.side_effect = RuntimeError("boom")
            result = self._run(store, svc)
            status, _ = _status(store, "c-hard")
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["duplicate_blocked"], [])
        self.assertEqual(status, "provisional")
        self.assertEqual(worker._exit_code({"fast_track_errors": result["errors"]}), 1)


if __name__ == "__main__":
    unittest.main()
