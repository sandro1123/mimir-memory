# -*- coding: utf-8 -*-
"""issue #11 第 2 步：ADD-only 闸收紧为「可拦阻」（默认仍只告警）。

## 为什么现在收紧

#11 自己的纪律写着：「先上只告警不拦形态观察，验证无噪声后再收紧为拦阻」。

**观察条件已满足**（2026-10-22 实测）：生产 `human_review` 积压 15 条，
其中携带 `possible_duplicate` 标记的 **0 条** —— 噪声为零。

## 设计约束

- **默认仍是 warn（零破坏）**：不设开关时行为与现在逐字节一致
- 开关 **按级别** 控制：只拦 `strong_warn`（Tier-0 精确命中）是最小可
  回退的一刀；`warn`（Tier-1 模糊）默认不拦——模糊判重误伤代价更高
- 被拦时**必须**留下可查的痕迹与明确错误，不能静默拒绝

TDD RED：`MIMIR_DEDUP_GATE` 与拒绝行为尚不存在。
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def _seed_fact(self, content):
        return self.store.create_fact(CreateFact(
            content=content, summary=content[:40], owner_principal=self.owner,
            domain="knowledge", fact_type="reference", visibility="all",
            sensitivity="internal", egress_policy="local_only",
            human_status="confirmed",
        ), actor_principal=self.owner)["fact_id"]

    def _dup_candidate(self, content, key):
        """造一条必然带 Tier-0 精确命中标记的候选。"""
        self._seed_fact(content)
        return self.svc.create_candidate(CreateCandidate(
            content=content, summary=content[:40],
            proposed_owner_principal=self.owner,
            proposed_domain="knowledge", proposed_fact_type="reference",
            source_id=None, source_hash=None, idempotency_key=f"cand-{key}",
        ), actor_principal=self.owner)

    def _approve(self, cid, key):
        return self.svc.review_candidate(
            ReviewCandidate(candidate_id=cid, action="approve",
                            reason="测试审批", idempotency_key=f"rv-{key}"),
            reviewer_principal=self.owner,
        )


class TestGateModeParsing(unittest.TestCase):
    def test_default_mode_is_warn(self):
        """不设环境变量 = warn，零破坏。"""
        from mimir_v8.dedup import gate_mode

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MIMIR_DEDUP_GATE", None)
            self.assertEqual(gate_mode(), "warn")

    def test_explicit_modes(self):
        from mimir_v8.dedup import gate_mode

        for val in ("warn", "block_strong", "off"):
            with mock.patch.dict(os.environ, {"MIMIR_DEDUP_GATE": val}):
                self.assertEqual(gate_mode(), val)

    def test_unknown_mode_raises(self):
        """写错的开关值必须炸，不能静默降级成默认——
        否则"我明明开了拦阻"却仍在放行。"""
        from mimir_v8.dedup import gate_mode

        with mock.patch.dict(os.environ, {"MIMIR_DEDUP_GATE": "block_all_typo"}):
            with self.assertRaises(ValueError):
                gate_mode()


class TestBlockMode(_Base):
    DUP = "Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储"

    def test_default_does_not_block(self):
        """默认 warn：重复候选照常批准（零破坏，已由 p16b 钉住，此处再确认）。"""
        c = self._dup_candidate(self.DUP, "d1")
        r = self._approve(c["candidate_id"], "d1")
        self.assertEqual(r["status"], "approved")

    def test_block_strong_rejects_exact_duplicate(self):
        """开 block_strong：Tier-0 精确重复必须被拒。"""
        from mimir_v8.candidates import DuplicateBlocked

        c = self._dup_candidate(self.DUP, "d2")
        with mock.patch.dict(os.environ, {"MIMIR_DEDUP_GATE": "block_strong"}):
            with self.assertRaises(DuplicateBlocked):
                self._approve(c["candidate_id"], "d2")

    def test_blocked_candidate_stays_reviewable(self):
        """被拦后候选不得被改成 approved——必须原样留在待审，
        否则人再也没法处理它（静默吞掉 = 比不拦更坏）。"""
        from mimir_v8.candidates import DuplicateBlocked

        c = self._dup_candidate(self.DUP, "d3")
        with mock.patch.dict(os.environ, {"MIMIR_DEDUP_GATE": "block_strong"}):
            with self.assertRaises(DuplicateBlocked):
                self._approve(c["candidate_id"], "d3")

        with contextlib.closing(self.store.connect()) as conn:
            row = conn.execute(
                "SELECT status FROM candidate_facts WHERE candidate_id=?",
                (c["candidate_id"],)).fetchone()
        self.assertEqual(row["status"], "review_required",
                         "被拦的候选必须保持可审状态")

    def test_off_mode_never_flags(self):
        """off：连告警都不留（完全静默，用于紧急回退）。"""
        c = self._dup_candidate(self.DUP, "d4")
        with mock.patch.dict(os.environ, {"MIMIR_DEDUP_GATE": "off"}):
            r = self._approve(c["candidate_id"], "d4")
        self.assertEqual(r["status"], "approved")
        with contextlib.closing(self.store.connect()) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='dedup.review_gate'"
            ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_block_leaves_audit_trace(self):
        """被拦必须留痕，且 outcome 标明是拦截而非告警。"""
        from mimir_v8.candidates import DuplicateBlocked

        c = self._dup_candidate(self.DUP, "d5")
        with mock.patch.dict(os.environ, {"MIMIR_DEDUP_GATE": "block_strong"}):
            with self.assertRaises(DuplicateBlocked):
                self._approve(c["candidate_id"], "d5")

        with contextlib.closing(self.store.connect()) as conn:
            rows = conn.execute(
                "SELECT outcome FROM audit_log WHERE action='dedup.review_gate' "
                "AND resource_id=?", (c["candidate_id"],)).fetchall()
        self.assertTrue(any(r["outcome"] == "blocked" for r in rows),
                        "拦截必须留 blocked 痕迹，否则事后无法审计")

    def test_block_strong_does_not_block_fuzzy(self):
        """block_strong **只**拦 Tier-0 精确；Tier-1 模糊仍只告警。

        模糊判重误伤代价更高（更新带 0.70~0.85 本就设计为"候选更新"），
        一刀切全拦会把合法的更新挡在门外。
        """
        near_a = "用户偏好新、环境好、带泳池等设施、价位与城堡酒店相当的酒店"
        near_b = "用户偏好选择较新、环境好、带泳池等设施、价位与城堡酒店相当的酒店"
        self._seed_fact(near_a)
        c = self.svc.create_candidate(CreateCandidate(
            content=near_b, summary=near_b[:40],
            proposed_owner_principal=self.owner,
            proposed_domain="knowledge", proposed_fact_type="reference",
            source_id=None, source_hash=None, idempotency_key="cand-fuzzy",
        ), actor_principal=self.owner)
        self.assertIsNotNone(c["duplicate_hint"], "前置：必须带模糊判重标记")

        with mock.patch.dict(os.environ, {"MIMIR_DEDUP_GATE": "block_strong"}):
            r = self._approve(c["candidate_id"], "fuzzy")
        self.assertEqual(r["status"], "approved", "模糊命中不得被 block_strong 拦")


if __name__ == "__main__":
    unittest.main()
