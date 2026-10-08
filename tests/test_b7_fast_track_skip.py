# -*- coding: utf-8 -*-
"""B7: fast-track 阈值跳过不触发 exit 1, 真异常仍触发.

置信度未过线是设计内去向 (留 provisional 待人审), 不是故障.
硬错误 (review/commit 抛异常) 仍进 errors → exit 1 (P1-11 加严保留).
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8 import worker
from mimir_v8.governance import fast_track_commit_all
from mimir_v8.store import CanonicalStore


def _insert_provisional(store, cid, confidence):
    now = "2026-10-09T00:00:00+00:00"
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_facts(candidate_id,content,summary,proposed_owner_principal,"
            "proposed_domain,proposed_fact_type,proposed_visibility,proposed_sensitivity,"
            "proposed_egress_policy,proposed_by,uncertainty_json,confidence_score,"
            "status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, f"content {cid}", f"summary {cid}", "mentor", "system", "pattern",
             "owner_only", "internal", "local_only", "service:test", "[]",
             confidence, "provisional", now, now),
        )


class TestFastTrackThresholdSkip(unittest.TestCase):
    def test_threshold_skip_not_error(self):
        """低置信候选 → skipped_below_threshold, 不进 errors."""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "ft-skip.db")
            _insert_provisional(store, "c-low", 0.1)
            with mock.patch.dict("os.environ", {"MIMIR_GOVERNANCE_AUTO_APPROVE": "1"}):
                import mimir_v8.governance as gov
                with mock.patch.object(gov, "GOVERNANCE_AUTO_APPROVE", True):
                    result = fast_track_commit_all(store, mock.Mock())
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["skipped_below_threshold"]), 1)
        self.assertEqual(result["committed"], 0)

    def test_exit_code_ignores_threshold_skip(self):
        """纯阈值跳过 → exit 0; 真异常 → exit 1."""
        self.assertEqual(
            worker._exit_code({"fast_track_errors": [
                {"candidate_id": "c", "error": "confidence below threshold"}]}), 0)
        self.assertEqual(
            worker._exit_code({"fast_track_errors": [
                {"candidate_id": "c", "error": "connection refused"}]}), 1)
        self.assertEqual(worker._exit_code({"errors": [{"x": 1}]}), 1)

    def test_hard_error_still_recorded(self):
        """commit 抛异常 → errors, 阈值跳过键不污染."""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "ft-hard.db")
            _insert_provisional(store, "c-high", 0.99)
            svc = mock.Mock()
            svc.review_candidate.side_effect = RuntimeError("boom")
            import mimir_v8.governance as gov
            with mock.patch.object(gov, "GOVERNANCE_AUTO_APPROVE", True):
                result = fast_track_commit_all(store, svc)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["skipped_below_threshold"], [])


if __name__ == "__main__":
    unittest.main()
