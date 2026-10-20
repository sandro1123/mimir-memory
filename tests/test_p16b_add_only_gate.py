# -*- coding: utf-8 -*-
"""roadmap #16 第二片：Tier-1 ADD-only 闸（issue #10）。

契约（roadmap 1.5.0 节选）：
- 判重**只准 ADD 语义**：Tier-1 命中不得自动改写/删除既有事实。
- warn-first：闸先只**告警+留痕**，不阻断审批（观察期、零破坏）。
- 闸必须接在真实路径上：判重 hint（``possible_duplicate:*``）必须被
  审批路径**消费**——写进去没人看等于没接线。

TDD RED：本文件引用的 ``duplicate_review_gate`` 等符号尚不存在。
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.candidates import CandidateService, CreateCandidate, ReviewCandidate
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class _Base(unittest.TestCase):
    owner = "mentor"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canon.db")
        self.svc = CandidateService(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_fact(self, content, owner=None):
        owner = owner or self.owner
        return self.store.create_fact(CreateFact(
            content=content, summary=content[:40], owner_principal=owner,
            fact_type="reference", domain="knowledge", visibility="all",
            sensitivity="internal", egress_policy="local_only",
            human_status="confirmed",
        ), actor_principal=owner)["fact_id"]

    def _cand(self, content, key, owner=None):
        owner = owner or self.owner
        cmd = CreateCandidate(
            content=content, proposed_owner_principal=owner,
            proposed_domain="knowledge", proposed_fact_type="reference",
            summary=content[:40],
            source_id=None, source_hash=None,
            idempotency_key=f"cand-{key}",
        )
        return self.svc.create_candidate(cmd, actor_principal=owner)


class TestGateSymbol(_Base):
    def test_gate_is_pure_and_detects_duplicate_marker(self):
        from mimir_v8.dedup import duplicate_review_gate

        hint = {"match_type": "update", "similarity": 0.82, "matched_fact_id": "f1"}
        verdict = duplicate_review_gate(hint)
        self.assertTrue(verdict["flagged"])
        self.assertEqual(verdict["level"], "warn")
        self.assertEqual(verdict["match_type"], "update")

    def test_gate_clean_when_no_hint(self):
        from mimir_v8.dedup import duplicate_review_gate

        self.assertFalse(duplicate_review_gate(None)["flagged"])
        self.assertFalse(duplicate_review_gate({})["flagged"])

    def test_tier0_merge_is_flagged_louder(self):
        """Tier-0 精确命中=同一内容，告警级别应高于 Tier-1 模糊。"""
        from mimir_v8.dedup import duplicate_review_gate

        v0 = duplicate_review_gate(
            {"match_type": "merge", "similarity": 1.0, "matched_fact_id": "f1", "tier": "tier0"})
        v1 = duplicate_review_gate(
            {"match_type": "update", "similarity": 0.75, "matched_fact_id": "f2", "tier": "tier1"})
        self.assertEqual(v0["level"], "strong_warn")
        self.assertEqual(v1["level"], "warn")


class TestGateWiredIntoApproval(_Base):
    def test_approve_leaves_audit_trace_when_duplicate(self):
        """判重 hint 必须被审批路径消费：approve 时留 audit 痕。"""
        self._seed_fact("Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储")
        c = self._cand("Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储", "dup1")
        self.assertIsNotNone(c["duplicate_hint"], "前置：候选写入时必须已带判重 hint")

        self.svc.review_candidate(
            ReviewCandidate(candidate_id=c["candidate_id"], action="approve",
                            reason="测试审批", idempotency_key="rv-dup1"),
            reviewer_principal="mentor",
        )
        with contextlib.closing(self.store.connect()) as conn:
            rows = conn.execute(
                "SELECT detail_json FROM audit_log WHERE action='dedup.review_gate' "
                "AND resource_id=?", (c["candidate_id"],)
            ).fetchall()
        self.assertEqual(len(rows), 1, "重复候选审批必须留 dedup.review_gate 痕")
        detail = json.loads(rows[0]["detail_json"])
        self.assertTrue(detail["flagged"])
        self.assertEqual(detail["match_type"], "merge")
        self.assertEqual(detail["level"], "strong_warn")

    def test_approve_is_not_blocked_warn_first(self):
        """warn-first：闸只告警，审批照常通过（零破坏）。"""
        self._seed_fact("Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储")
        c = self._cand("Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储", "dup2")
        r = self.svc.review_candidate(
            ReviewCandidate(candidate_id=c["candidate_id"], action="approve",
                            reason="warn-first 不阻断", idempotency_key="rv-dup2"),
            reviewer_principal="mentor",
        )
        self.assertEqual(r["status"], "approved")

    def test_clean_candidate_leaves_no_gate_trace(self):
        """无重复标记的候选：不得产生闸痕（否则噪声淹没有效信号）。"""
        c = self._cand("一条完全独立的新事实：服务器机房温度常年 22 度", "clean1")
        self.assertIsNone(c["duplicate_hint"])
        self.svc.review_candidate(
            ReviewCandidate(candidate_id=c["candidate_id"], action="approve",
                            reason="干净审批", idempotency_key="rv-clean1"),
            reviewer_principal="mentor",
        )
        with contextlib.closing(self.store.connect()) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='dedup.review_gate'"
            ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_add_only_existing_fact_survives_commit(self):
        """ADD-only 不变量：commit 后既有事实仍 active，内容不被改写。"""
        fid = self._seed_fact("Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储")
        c = self._cand("Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储", "dup3")
        self.svc.review_candidate(
            ReviewCandidate(candidate_id=c["candidate_id"], action="approve",
                            reason="ADD-only 验证", idempotency_key="rv-dup3"),
            reviewer_principal="mentor",
        )
        committed = self.svc.commit_approved(c["candidate_id"], "mentor", "cm-dup3")
        self.assertNotEqual(committed["fact_id"], fid, "ADD-only：必须是新事实，不是改写旧事实")
        with contextlib.closing(self.store.connect()) as conn:
            old = conn.execute(
                "SELECT status, content, current_version FROM facts WHERE fact_id=?", (fid,)
            ).fetchone()
        self.assertEqual(old["status"], "active", "既有事实不得被闸改写/失效")
        self.assertEqual(old["current_version"], 1, "既有事实版本不得变动")
        self.assertEqual(old["content"], "Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储")


if __name__ == "__main__":
    unittest.main()
