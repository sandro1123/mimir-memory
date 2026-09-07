# -*- coding: utf-8 -*-
"""P47 治理链韧性：环境变量回退 / LLM 失败不再无声 / requeue 上限 / worker 退出码。

审计 2026-09-07 P0-1：治理单元从未配 MIMIR_ROUTER_*，抽取单元只有 MIMIR_EVALUATOR_*；
v10 硬编码 key 被开源脱敏后治理静默失效 6 天，18 条候选每 15 分钟被 requeue 一轮
（7 天 7836 条 candidate.requeued 事件），journal 零错、health 恒 ok。
"""
import contextlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mimir_v8 import governance
from mimir_v8 import worker
from mimir_v8.store import CanonicalStore, utc_now


def _insert_candidate(store, cid, status="human_review"):
    now = utc_now()
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_facts(candidate_id,content,summary,proposed_owner_principal,"
            "proposed_domain,proposed_fact_type,proposed_visibility,proposed_sensitivity,"
            "proposed_egress_policy,proposed_by,uncertainty_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, f"content {cid}", f"summary {cid}", "mentor", "system", "pattern", "owner_only",
             "internal", "local_only", "service:test", "[]", status, now, now),
        )


def _insert_failed_assessment(store, cid, created_at):
    with store.transaction() as c:
        c.execute(
            "INSERT INTO candidate_review_assessments(assessment_id,candidate_id,reviewer_type,"
            "provider,model,recommendation,risk,confidence,is_valuable,is_noise,domain,fact_type,"
            "summary,reasoning,raw_output_hash,success,error_code,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"a-{cid}-{created_at}", cid, "llm", "router", "", "unknown", "medium", 0.0, 0, 0,
             "personal", "user_pref", "", "", "", 0, "LLM 不可用", created_at),
        )


class TestRouterConfigFallback(unittest.TestCase):
    def test_falls_back_to_evaluator_env(self):
        env = {
            "MIMIR_EVALUATOR_API_URL": "http://127.0.0.1:20128/v1",
            "MIMIR_EVALUATOR_API_KEY": "sk-test",
            "MIMIR_EVALUATOR_MODEL": "m/x",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = governance.router_config()
        self.assertEqual(cfg["url"], "http://127.0.0.1:20128/v1")
        self.assertEqual(cfg["api_key"], "sk-test")
        self.assertEqual(cfg["primary_model"], "m/x")
        self.assertEqual(cfg["fallback_model"], "m/x")

    def test_router_env_wins(self):
        env = {
            "MIMIR_ROUTER_API_KEY": "sk-router",
            "MIMIR_EVALUATOR_API_KEY": "sk-eval",
            "MIMIR_GOVERNANCE_MODEL": "gov",
            "MIMIR_EVALUATOR_MODEL": "eva",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = governance.router_config()
        self.assertEqual(cfg["api_key"], "sk-router")
        self.assertEqual(cfg["primary_model"], "gov")


class TestLlmErrorsAreHonest(unittest.TestCase):
    def test_missing_key_reports_cause(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result, error = governance._call_llm("p", "m")
        self.assertIsNone(result)
        self.assertIn("api_key", error)

    def test_transport_error_reports_cause(self):
        with mock.patch.dict("os.environ", {"MIMIR_ROUTER_API_KEY": "sk-x"}, clear=True), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("connection refused")), \
             self.assertLogs("mimir_v8.governance", level="WARNING") as logs:
            result, error = governance._call_llm("p", "m")
        self.assertIsNone(result)
        self.assertIn("OSError", error)
        self.assertTrue(any("connection refused" in line for line in logs.output))

    def test_assess_candidate_error_carries_cause(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            a = governance.assess_candidate("普通内容 no markers", "cid-1")
        self.assertFalse(a.success)
        self.assertTrue(a.error.startswith("LLM 不可用"))
        self.assertIn("api_key", a.error)


class TestRequeueCap(unittest.TestCase):
    def test_only_unassessed_skips_candidates_over_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            _insert_candidate(store, "fresh")
            _insert_candidate(store, "worn")
            recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            for i in range(3):
                _insert_failed_assessment(store, "worn", recent[:-1] + str(i))
            old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
            _insert_candidate(store, "rested")
            for i in range(5):
                _insert_failed_assessment(store, "rested", old[:-1] + str(i))
            r = worker.review_requeue(store, "service:governance", only_unassessed=True)
            self.assertEqual(r["requeued"], 2)
            with contextlib.closing(store.connect()) as c:
                rows = dict(c.execute("SELECT candidate_id, status FROM candidate_facts").fetchall())
            self.assertEqual(rows["fresh"], "review_required")
            self.assertEqual(rows["rested"], "review_required")
            self.assertEqual(rows["worn"], "human_review")


class TestWorkerExitCode(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(worker._exit_code({"created": [], "failed": []}), 0)
        self.assertEqual(worker._exit_code({"errors": [{"source": "x"}]}), 1)
        self.assertEqual(worker._exit_code({"failed": [{"run_id": "r"}]}), 1)
        self.assertEqual(worker._exit_code({"llm_failures": 3, "processed": 3}), 1)
        self.assertEqual(worker._exit_code({"llm_failures": 0, "processed": 3}), 0)
        self.assertEqual(worker._exit_code("not a dict"), 0)


if __name__ == "__main__":
    unittest.main()
