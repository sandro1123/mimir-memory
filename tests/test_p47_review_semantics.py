# -*- coding: utf-8 -*-
"""P47 人审语义：候选 approve 落库后事实的 human_status。

审计 2026-09-07 P1-3：262 条 approve（admin 人工 128 + service:governance 134）落库后
事实全部 human_status=unreviewed（389/410 active），人审信号在 commit 一步丢失，
是金标老化（unreviewed / confidence None / L4）的病根。
规则：人（非 service:*）批准 → confirmed；治理自动批准 → 仍 unreviewed。
"""
import tempfile
import unittest
from pathlib import Path

from mimir_v8.candidates import CandidateService, ReviewCandidate
from mimir_v8.store import CanonicalStore, utc_now


def _insert_candidate(store, cid):
    now = utc_now()
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_facts(candidate_id,content,summary,proposed_owner_principal,"
            "proposed_domain,proposed_fact_type,proposed_visibility,proposed_sensitivity,"
            "proposed_egress_policy,proposed_by,uncertainty_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, f"content {cid}", f"summary {cid}", "mentor", "system", "pattern", "owner_only",
             "internal", "local_only", "service:test", "[]", "review_required", now, now),
        )


class TestApproveSetsHumanStatus(unittest.TestCase):
    def _commit(self, reviewer):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = CanonicalStore(Path(tmp.name) / "canonical.db")
        svc = CandidateService(store)
        _insert_candidate(store, "c1")
        svc.review_candidate(
            ReviewCandidate(candidate_id="c1", action="approve", reason="ok", idempotency_key="ik-1"),
            reviewer,
        )
        out = svc.commit_approved("c1", actor_principal=reviewer, idempotency_key="ik-2")
        return store.get_fact(out["fact_id"])

    def test_human_reviewer_confirms(self):
        self.assertEqual(self._commit("admin")["human_status"], "confirmed")

    def test_service_reviewer_stays_unreviewed(self):
        self.assertEqual(self._commit("service:governance")["human_status"], "unreviewed")


if __name__ == "__main__":
    unittest.main()
