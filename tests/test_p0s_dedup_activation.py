# -*- coding: utf-8 -*-
"""③-4 判重接通：候选写入时标记疑似重复（观察期，不阻断）。"""
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.candidates import CandidateService, CreateCandidate
from mimir_v8.store import CanonicalStore


def _seed_fact(store, content, owner="mentor"):
    from mimir_v8.schema import CreateFact
    return store.create_fact(CreateFact(
        content=content, summary="摘要", owner_principal=owner,
        domain="knowledge", fact_type="reference", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)


class TestDedupActivation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")
        self.svc = CandidateService(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _cand(self, content, key):
        return self.svc.create_candidate(CreateCandidate(
            content=content, summary="s", proposed_owner_principal="mentor",
            proposed_domain="knowledge", proposed_fact_type="reference",
            source_id=None, source_hash=None, idempotency_key=key,
        ), actor_principal="mentor")

    def test_near_duplicate_candidate_is_flagged_not_blocked(self):
        _seed_fact(self.store, "用户偏好新、环境好、带泳池等设施、价位与城堡酒店相当的酒店")
        r = self._cand("用户偏好选择较新、环境好、带泳池等设施、价位与城堡酒店相当的酒店", "k1")
        self.assertTrue(r.get("duplicate_hint"), "近重复候选必须被标记")
        # 实测：这对字符串 Jaccard=0.8214，落在 update 带（0.70~0.85）。
        # 计划书此处写 "merge" 是未实测的笔误——不为了迁就该字面量去
        # 调种子数据（那等于把测试改成迎合断言）。
        self.assertEqual(r["duplicate_hint"]["match_type"], "update")
        # 带位必须与自报 similarity 和 dedup 阈值自洽（钉住分带逻辑）
        from mimir_v8.dedup import JACCARD_MERGE_THRESHOLD, JACCARD_UPDATE_THRESHOLD
        sim = r["duplicate_hint"]["similarity"]
        self.assertGreaterEqual(sim, JACCARD_UPDATE_THRESHOLD)
        self.assertLess(sim, JACCARD_MERGE_THRESHOLD)
        # 关键：候选仍然被创建（观察期不阻断）
        self.assertEqual(r["status"], "review_required")
        with contextlib.closing(self.store.connect()) as c:
            n = c.execute("SELECT COUNT(*) FROM candidate_facts WHERE candidate_id=?",
                          (r["candidate_id"],)).fetchone()[0]
        self.assertEqual(n, 1, "标记不得阻断候选创建")

    def test_unique_candidate_has_no_hint(self):
        _seed_fact(self.store, "生产库只允许经 API 写入")
        r = self._cand("今天天气不错适合出门散步", "k2")
        self.assertIsNone(r.get("duplicate_hint"), "非重复候选不得有标记")

    def test_no_facts_at_all_is_safe(self):
        r = self._cand("库里什么都没有时的第一条", "k3")
        self.assertIsNone(r.get("duplicate_hint"))

    def test_check_failure_does_not_block_candidate(self):
        """判重异常必须 fail-open——观察件绝不阻断写入。"""
        from unittest import mock
        import mimir_v8.candidates as cand_mod
        with mock.patch.object(cand_mod, "check_duplicate",
                               side_effect=RuntimeError("boom")):
            r = self._cand("判重炸了也要能写进去", "k4")
        self.assertEqual(r["status"], "review_required", "判重失败不得阻断")
        self.assertIsNone(r["duplicate_hint"], "判重失败不得伪造标记")
        # 事务必须真的提交了（不是只把 dict 返回给调用方）
        with contextlib.closing(self.store.connect()) as c:
            n = c.execute("SELECT COUNT(*) FROM candidate_facts WHERE candidate_id=?",
                          (r["candidate_id"],)).fetchone()[0]
            a = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='dedup.candidate_check'").fetchone()[0]
        self.assertEqual(n, 1, "判重失败时候选必须已落库")
        self.assertEqual(a, 0, "判重失败不得留审计痕")

    def test_audit_trail_written_on_hit(self):
        _seed_fact(self.store, "用户偏好新、环境好、带泳池等设施、价位与城堡酒店相当的酒店")
        r = self._cand("用户偏好选择较新、环境好、带泳池等设施、价位与城堡酒店相当的酒店", "k5")
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute(
                "SELECT resource_id, outcome, detail_json FROM audit_log "
                "WHERE action='dedup.candidate_check'").fetchall()
        self.assertGreaterEqual(len(rows), 1, "命中必须留审计痕")
        # 留痕必须挂在本次候选上、含匹配详情，不是一条空痕
        self.assertIn(r["candidate_id"], {row[0] for row in rows})
        import json
        detail = json.loads(rows[0][2])
        self.assertEqual(detail["match_type"], "update")
        self.assertEqual(detail["candidate_id"], r["candidate_id"])
        self.assertTrue(detail["matched_fact_id"])

    def test_hint_also_marked_into_uncertainty_json(self):
        """标记须落进候选的 uncertainty_json（append，不覆盖既有 reasons）。"""
        _seed_fact(self.store, "用户偏好新、环境好、带泳池等设施、价位与城堡酒店相当的酒店")
        r = self.svc.create_candidate(CreateCandidate(
            content="用户偏好选择较新、环境好、带泳池等设施、价位与城堡酒店相当的酒店",
            summary="s", proposed_owner_principal="mentor",
            proposed_domain="knowledge", proposed_fact_type="reference",
            uncertainty_reasons=("原有理由A",), idempotency_key="ku",
        ), actor_principal="mentor")
        with contextlib.closing(self.store.connect()) as c:
            raw = c.execute("SELECT uncertainty_json FROM candidate_facts WHERE candidate_id=?",
                            (r["candidate_id"],)).fetchone()[0]
        import json
        reasons = json.loads(raw)
        self.assertIn("原有理由A", reasons, "既有 uncertainty_reasons 不得被覆盖")
        self.assertTrue(any(str(x).startswith("possible_duplicate:update:") for x in reasons),
                        f"未追加 marked reason: {reasons}")

    def test_unique_candidate_writes_no_dedup_audit(self):
        """非重复不得留痕——否则审计面被噪音灌满，观察数据失真。"""
        _seed_fact(self.store, "生产库只允许经 API 写入")
        self._cand("今天天气不错适合出门散步", "k6")
        with contextlib.closing(self.store.connect()) as c:
            n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='dedup.candidate_check'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_broken_hint_value_does_not_block_candidate(self):
        """判重返回畸形值时标记落库会炸——必须只回滚标记，不拖死候选写入。

        判重是观察件：故障面不只是「判重调用本身」，还包括「把判重结果
        写进库」这一步。两者都必须 fail-open。
        """
        from unittest import mock
        import mimir_v8.candidates as cand_mod
        bad = {"is_duplicate": True, "similarity": object(),  # 不可 JSON 序列化
               "match_type": "merge", "matched_fact_id": "f1",
               "matched_content": "x"}
        with mock.patch.object(cand_mod, "check_duplicate", return_value=bad):
            r = self._cand("判重返回畸形值", "kbad")
        self.assertEqual(r["status"], "review_required", "标记落库失败不得阻断候选")
        self.assertIsNone(r["duplicate_hint"], "落库失败的标记不得上报为已标记")
        with contextlib.closing(self.store.connect()) as c:
            n = c.execute("SELECT COUNT(*) FROM candidate_facts WHERE candidate_id=?",
                          (r["candidate_id"],)).fetchone()[0]
            a = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='dedup.candidate_check'").fetchone()[0]
        self.assertEqual(n, 1, "候选必须已落库")
        self.assertEqual(a, 0, "落库失败的审计痕不得残留")
        # 事务必须仍然可用（savepoint 回滚没把外层事务弄坏）
        self.assertIsNone(self._cand("事务仍可用", "kafter")["duplicate_hint"])

    def test_marker_savepoint_failure_still_fails_open(self):
        """双故障：连 SAVEPOINT 都开不成时，回滚语句本身会再抛。

        标记失败路径不允许「清理动作」把异常带出——观察件的 fail-open
        必须是兜底的，否则最坏情况仍是阻断写入。
        """
        from unittest import mock
        import contextlib
        import mimir_v8.candidates as cand_mod
        bad = {"is_duplicate": True, "similarity": 0.9, "match_type": "merge",
               "matched_fact_id": "f1", "matched_content": "x"}

        class _BoomConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args, **kwargs):
                head = sql.strip().upper()
                if head.startswith("SAVEPOINT"):
                    raise RuntimeError("savepoint unavailable")
                if head.startswith("ROLLBACK TO"):
                    raise RuntimeError("no such savepoint")
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        real_transaction = self.store.transaction

        @contextlib.contextmanager
        def patched_transaction():
            with real_transaction() as conn:
                yield _BoomConn(conn)

        with mock.patch.object(self.store, "transaction", patched_transaction), \
                mock.patch.object(cand_mod, "check_duplicate", return_value=bad):
            r = self._cand("双故障也必须写进去", "ksb")
        self.assertEqual(r["status"], "review_required", "标记清理失败不得阻断候选")
        self.assertIsNone(r["duplicate_hint"])
        with contextlib.closing(self.store.connect()) as c:
            n = c.execute("SELECT COUNT(*) FROM candidate_facts WHERE candidate_id=?",
                          (r["candidate_id"],)).fetchone()[0]
        self.assertEqual(n, 1, "候选必须已落库")

    def test_replay_path_also_carries_hint_key(self):
        """两处 return 必须同形态——重放路径也要有 duplicate_hint 键（值 None）。"""
        r1 = self._cand("重放测试内容", "kr")
        r2 = self._cand("重放测试内容", "kr")   # 同幂等键 → replay
        self.assertTrue(r2.get("idempotent_replay"), "必须走重放路径")
        self.assertIn("duplicate_hint", r2, "重放路径也必须有该键（值 None）")
        self.assertIn("duplicate_hint", r1)


if __name__ == "__main__":
    unittest.main()
