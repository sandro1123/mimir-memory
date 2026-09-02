"""Mímir v12.2.0 — XTMEM strict lineage inheritance (spec 阶段二任务3).

派生事实（supersedes 候选）的 visibility/sensitivity/egress_policy
强制继承来源事实与提案中最严格的一档；无有效来源一律 Fail-Closed
（拒绝创建，绝不静默放宽）。

XTMEM Lineage Gate: derived knowledge can never be more permissive
than any of its sources. The gate lives in
CandidateService.create_candidate_in_transaction — the single choke
point every derived candidate passes through (learning.correct,
extraction, governance fast-track).

TDD RED → GREEN.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.candidates import (
    CandidateService,
    CandidatePolicyError,
    CreateCandidate,
)
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _seed(store, content, *, visibility="all", sensitivity="internal",
          egress="external_allowed", fact_type="event", owner="mentor"):
    result = store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain="knowledge",
            fact_type=fact_type,
            visibility=visibility,
            sensitivity=sensitivity,
            egress_policy=egress,
            human_status="confirmed",
        ),
        actor_principal=owner,
    )
    return result["fact_id"]


def _candidate(service, content, *, supersedes=None, visibility="all",
               sensitivity="internal", egress="external_allowed", key="k"):
    return CreateCandidate(
        content=content,
        proposed_owner_principal="mentor",
        proposed_domain="knowledge",
        proposed_fact_type="event",
        proposed_visibility=visibility,
        proposed_sensitivity=sensitivity,
        proposed_egress_policy=egress,
        supersedes_fact_id=supersedes,
        idempotency_key=key,
    )


class LineageFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.service = CandidateService(self.store)

    def create(self, command, actor="mentor"):
        return self.service.create_candidate(command, actor)

    def row(self, candidate_id, column):
        with self.store.connect() as conn:
            return conn.execute(
                f"SELECT {column} FROM candidate_facts WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()[0]


class TestStrictLineageInheritance(unittest.TestCase):
    """派生候选三档继承来源与提案中更严的一档。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = LineageFixture(Path(self._tmp.name))
        # 来源事实：最严格的一档
        self.strict_id = _seed(
            self.fx.store, "来源：严格铁律事实",
            visibility="owner_only", sensitivity="restricted",
            egress="local_only",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_loose_proposal_inherits_strict_source(self):
        # 提案全宽松，来源全严 → 全部继承严档
        result = self.fx.create(_candidate(
            self.fx.service, "修正后的内容",
            supersedes=self.strict_id,
            visibility="all", sensitivity="internal",
            egress="external_allowed", key="lin-1",
        ))
        cid = result["candidate_id"]
        self.assertEqual(self.fx.row(cid, "proposed_visibility"), "owner_only")
        self.assertEqual(self.fx.row(cid, "proposed_sensitivity"), "restricted")
        self.assertEqual(self.fx.row(cid, "proposed_egress_policy"), "local_only")

    def test_stricter_proposal_stays_stricter(self):
        # 提案比来源更严 → 保留提案（继承=max(来源, 提案)，不是无条件抄来源）
        loose_id = _seed(
            self.fx.store, "来源：宽松事实",
            visibility="all", sensitivity="internal",
            egress="external_allowed",
        )
        result = self.fx.create(_candidate(
            self.fx.service, "加密后的修正内容",
            supersedes=loose_id,
            visibility="owner_only", sensitivity="restricted",
            egress="local_only", key="lin-2",
        ))
        cid = result["candidate_id"]
        self.assertEqual(self.fx.row(cid, "proposed_visibility"), "owner_only")
        self.assertEqual(self.fx.row(cid, "proposed_sensitivity"), "restricted")
        self.assertEqual(self.fx.row(cid, "proposed_egress_policy"), "local_only")

    def test_mixed_inheritance_picks_stricter_per_field(self):
        # 三档独立仲裁：来源严两档、提案严一档 → 每档各取其严
        mixed_id = _seed(
            self.fx.store, "来源：混合档事实",
            visibility="shared", sensitivity="internal",
            egress="local_only",
        )
        result = self.fx.create(_candidate(
            self.fx.service, "混合修正",
            supersedes=mixed_id,
            visibility="all", sensitivity="confidential",
            egress="external_allowed", key="lin-3",
        ))
        cid = result["candidate_id"]
        self.assertEqual(self.fx.row(cid, "proposed_visibility"), "shared")
        self.assertEqual(self.fx.row(cid, "proposed_sensitivity"), "confidential")
        self.assertEqual(self.fx.row(cid, "proposed_egress_policy"), "local_only")

    def test_no_supersedes_keeps_proposal(self):
        # 非派生候选（无 supersedes）不受血缘门约束
        result = self.fx.create(_candidate(
            self.fx.service, "独立新事实", key="lin-4",
        ))
        cid = result["candidate_id"]
        self.assertEqual(self.fx.row(cid, "proposed_visibility"), "all")


class TestLineageFailClosed(unittest.TestCase):
    """无来源（supersedes 指向不存在的事实）一律 Fail-Closed。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = LineageFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_supersedes_rejected(self):
        with self.assertRaises(CandidatePolicyError):
            self.fx.create(_candidate(
                self.fx.service, "指向幽灵的修正",
                supersedes="fact-does-not-exist", key="lin-5",
            ))

    def test_fail_closed_writes_nothing(self):
        # 拒绝后不留任何候选行（非静默降级）
        before = self.fx.store.counts()["facts"]
        try:
            self.fx.create(_candidate(
                self.fx.service, "指向幽灵的修正",
                supersedes="fact-does-not-exist", key="lin-6",
            ))
            self.fail("expected CandidatePolicyError")
        except CandidatePolicyError:
            pass
        with self.fx.store.connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM candidate_facts WHERE idempotency_key=?"
                if False else
                "SELECT COUNT(*) FROM candidate_facts WHERE proposed_by='mentor'"
            ).fetchone()
        self.assertEqual(rows[0], 0)
        self.assertEqual(self.fx.store.counts()["facts"], before)

    def test_idempotent_replay_still_works_with_gate(self):
        # 门不影响幂等重放：先建合法派生候选，同 key 重放拿同一 candidate
        src = _seed(self.fx.store, "合法来源", key_owner := "mentor") if False else _seed(self.fx.store, "合法来源")
        cmd = _candidate(self.fx.service, "第一次", supersedes=src, key="lin-7")
        first = self.fx.create(cmd)
        second = self.fx.create(cmd)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["candidate_id"], second["candidate_id"])


class TestLineageCommitCarriesInheritance(unittest.TestCase):
    """继承必须贯穿到 commit：落库事实带继承后的严档。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = LineageFixture(Path(self._tmp.name))
        self.strict_id = _seed(
            self.fx.store, "来源事实",
            visibility="owner_only", sensitivity="restricted",
            egress="local_only",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_committed_fact_inherits_strictness(self):
        from mimir_v8.candidates import ReviewCandidate
        result = self.fx.create(_candidate(
            self.fx.service, "待审批的修正",
            supersedes=self.strict_id,
            visibility="all", sensitivity="internal",
            egress="external_allowed", key="lin-8",
        ))
        cid = result["candidate_id"]
        self.fx.service.review_candidate(
            ReviewCandidate(candidate_id=cid, action="approve",
                            reason="lineage test", idempotency_key="rev-8"),
            "mentor",
        )
        committed = self.fx.service.commit_approved(
            cid, idempotency_key="commit-8", actor_principal="mentor")
        fact = self.fx.store.get_fact(committed["fact_id"])
        self.assertEqual(fact["visibility"], "owner_only")
        self.assertEqual(fact["sensitivity"], "restricted")
        self.assertEqual(fact["egress_policy"], "local_only")


if __name__ == "__main__":
    unittest.main()
