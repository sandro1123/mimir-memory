# -*- coding: utf-8 -*-
"""③-5 Reflect 主动反思（1.3.0）：LLM 提炼洞察 → 走治理管线（候选队列）。

核心契约（派工 brief task-1）：
1. 按 domain 取窗口内 active facts（上限 40 条防 prompt 爆炸）
2. 少于 MIN_FACTS_PER_DOMAIN 的 domain 跳过（料太少无法提炼）
3. LLM 输出严格 JSON：{"insights": [{"kind","text","confidence"}]}
4. 每条洞察 → CandidateService.create_candidate（绝不直接写 facts）
5. LLM 失败 → 计数 + 继续下一 domain（不中断整轮、不 raise）
6. 账本 reflect_runs（守卫式建表，照抄 crystal_runs 模式）

TDD RED → GREEN.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.reflect import ReflectionService
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _seed(store, content, domain="knowledge", owner="mentor"):
    return store.create_fact(CreateFact(
        content=content, summary="s", owner_principal=owner, domain=domain,
        fact_type="reference", visibility="all", sensitivity="internal",
        egress_policy="local_only", human_status="confirmed",
    ), actor_principal=owner)


def _fake_llm(payload):
    def _call(prompt, model):
        return payload, None
    return _call


class TestReflectionScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")
        for i in range(6):
            _seed(self.store, f"知识条目 {i}：Mímir 的记忆治理设计要点 {i}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_insight_becomes_candidate_not_fact(self):
        """核心契约：洞察进候选队列，绝不直接写 facts。"""
        payload = {"insights": [{"kind": "pattern", "text": "多条知识都指向治理优先", "confidence": 0.8}]}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        self.assertEqual(r["insights_created"], 1)
        with contextlib.closing(self.store.connect()) as c:
            cands = c.execute("SELECT content, proposed_fact_type, uncertainty_json FROM candidate_facts").fetchall()
            facts_before = 6
            facts_now = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(len(cands), 1, "洞察必须落成候选")
        self.assertEqual(cands[0][1], "pattern")
        self.assertIn("reflect:pattern", cands[0][2])
        self.assertEqual(facts_now, facts_before, "洞察绝不得直接写 facts")

    def test_llm_failure_degrades_honestly(self):
        with mock.patch("mimir_v8.reflect._call_llm", lambda p, m: (None, "URLError: refused")):
            r = ReflectionService(self.store).scan()
        self.assertEqual(r["insights_created"], 0)
        self.assertGreaterEqual(r["llm_failures"], 1, "失败必须计数上报")

    def test_sparse_domain_is_skipped(self):
        _seed(self.store, "孤零零一条", domain="quant")
        payload = {"insights": [{"kind": "pattern", "text": "x", "confidence": 0.5}]}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        # knowledge 有 6 条会被扫；quant 只有 1 条应被跳过
        self.assertLessEqual(r["domains_scanned"], 6)
        self.assertGreaterEqual(r["skipped"], 1, "料太少的 domain 必须跳过")

    def test_run_ledger_written(self):
        payload = {"insights": []}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute("SELECT status FROM reflect_runs ORDER BY started_at").fetchall()
        self.assertTrue(rows, "账本必须有痕")
        self.assertEqual(rows[-1][0], "completed")


class TestReflectionContracts(unittest.TestCase):
    """补充契约：窗口/上限/跳过阈值/幂等/账本失败面/返回结构。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canonical.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_window_rejects_sub_day(self):
        with self.assertRaises(ValueError):
            ReflectionService(self.store).scan(window_days=0)

    def test_scan_returns_documented_keys(self):
        payload = {"insights": []}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        for key in ("run_id", "domains_scanned", "insights_created",
                    "llm_failures", "skipped", "runs"):
            self.assertIn(key, r)
        self.assertIn("catchup", r["runs"])

    def test_domain_below_threshold_is_skipped_not_called(self):
        """少于 MIN_FACTS_PER_DOMAIN 的 domain 连 LLM 都不该调。"""
        for i in range(4):
            _seed(self.store, f"仅四条 {i}", domain="system")
        calls = []

        def _spy(prompt, model):
            calls.append(prompt)
            return {"insights": []}, None

        with mock.patch("mimir_v8.reflect._call_llm", _spy):
            r = ReflectionService(self.store).scan()
        self.assertEqual(calls, [], "料太少不该浪费 LLM 调用")
        self.assertEqual(r["domains_scanned"], 0)
        self.assertGreaterEqual(r["skipped"], 1)

    def test_insight_idempotency_key_is_stable_and_deduped(self):
        """同一 run 重放同一幂等键不得产生重复候选。"""
        for i in range(6):
            _seed(self.store, f"幂等内容 {i}")
        payload = {"insights": [{"kind": "gap", "text": "缺 X 域", "confidence": 0.4}]}
        service = ReflectionService(self.store)
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            first = service.scan()
            second = service.scan(run_id=first["run_id"])
        with contextlib.closing(self.store.connect()) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM candidate_facts").fetchone()[0]
            keys = [r[0] for r in c.execute(
                "SELECT idempotency_key FROM memory_events"
                " WHERE event_type='candidate.created'").fetchall()]
        # 第二次是幂等重放：不新增候选
        self.assertEqual(second["insights_created"], 0)
        self.assertEqual(count, 1)
        self.assertEqual(keys, [f"reflect:{first['run_id']}:knowledge:0"],
                         "幂等键必须含 run_id 且形如 reflect:<run>:<domain>:<idx>")

    def test_distinct_runs_produce_distinct_candidates(self):
        """run_id 必须参与幂等键——否则不同轮次的同一洞察会被永久吞掉。"""
        for i in range(6):
            _seed(self.store, f"跨轮内容 {i}")
        payload = {"insights": [{"kind": "pattern", "text": "跨轮同一条", "confidence": 0.7}]}
        service = ReflectionService(self.store)
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            first = service.scan()
            second = service.scan()
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["insights_created"], 1)
        self.assertEqual(second["insights_created"], 1,
                         "不同 run 的洞察必须各自落候选（键含 run_id）")

    def test_llm_failure_then_next_domain_continues(self):
        """一个 domain 失败不得中断整轮——其余 domain 照常产出。"""
        for i in range(6):
            _seed(self.store, f"知识 {i}", domain="knowledge")
        for i in range(6):
            _seed(self.store, f"个人偏好 {i}", domain="personal")
        seen = []

        def _flaky(prompt, model):
            # 第一个 domain 失败，其余成功
            if not seen:
                seen.append(prompt)
                return None, "TimeoutError: read timeout"
            seen.append(prompt)
            return {"insights": [{"kind": "pattern", "text": "第二域模式", "confidence": 0.7}]}, None

        with mock.patch("mimir_v8.reflect._call_llm", _flaky):
            r = ReflectionService(self.store).scan(max_domains=2)
        self.assertEqual(r["domains_scanned"], 2, "两个域都必须被扫过")
        self.assertEqual(r["llm_failures"], 1)
        self.assertEqual(r["insights_created"], 1, "失败的域之后仍要产出")

    def test_max_domains_bounds_the_work(self):
        for domain in ("knowledge", "personal", "system"):
            for i in range(6):
                _seed(self.store, f"{domain}-{i}", domain=domain)
        calls = []

        def _spy(prompt, model):
            calls.append(prompt)
            return {"insights": []}, None

        with mock.patch("mimir_v8.reflect._call_llm", _spy):
            r = ReflectionService(self.store).scan(max_domains=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["domains_scanned"], 1)

    def test_facts_cap_limits_prompt_size(self):
        for i in range(45):
            _seed(self.store, f"超量事实 {i}")
        captured = {}

        def _spy(prompt, model):
            captured["prompt"] = prompt
            return {"insights": []}, None

        with mock.patch("mimir_v8.reflect._call_llm", _spy):
            ReflectionService(self.store).scan()
        self.assertIn("超量事实 44", captured["prompt"], "最新的必须在")
        self.assertEqual(
            captured["prompt"].count("超量事实"), 40,
            "prompt 必须被 MAX_FACTS_PER_PROMPT 截断（45 条只能进 40 条）")

    def test_each_insight_becomes_its_own_candidate(self):
        for i in range(6):
            _seed(self.store, f"多条洞察 {i}")
        payload = {"insights": [
            {"kind": "pattern", "text": "模式一", "confidence": 0.9},
            {"kind": "contradiction", "text": "矛盾一", "confidence": 0.6},
            {"kind": "gap", "text": "缺口一", "confidence": 0.3},
        ]}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        self.assertEqual(r["insights_created"], 3)
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute(
                "SELECT uncertainty_json FROM candidate_facts ORDER BY content"
            ).fetchall()
        reasons = [json.loads(row[0]) for row in rows]
        self.assertIn(["reflect:contradiction"], reasons)
        self.assertIn(["reflect:gap"], reasons)

    def test_malformed_insights_are_tolerated(self):
        """LLM 吐畸形条目不得炸整轮，也不得产出垃圾候选。"""
        for i in range(6):
            _seed(self.store, f"畸形输入 {i}")
        payload = {"insights": [
            {"kind": "pattern", "text": "好的一条", "confidence": 0.5},
            {"kind": "bogus_kind", "text": "坏 kind", "confidence": 0.5},
            {"kind": "gap", "text": "   ", "confidence": 0.5},
            "not-a-dict",
        ]}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(self.store).scan()
        self.assertEqual(r["insights_created"], 1)

    def test_env_overrides_window_and_max_domains(self):
        """MIMIR_REFLECT_* 是 Task 2 配置门的旋钮，读法在这里钉死。"""
        for i in range(6):
            _seed(self.store, f"环境变量 {i}")
        calls = []

        def _spy(prompt, model):
            calls.append(prompt)
            return {"insights": []}, None

        with mock.patch.dict(os.environ, {
                "MIMIR_REFLECT_WINDOW_DAYS": "30",
                "MIMIR_REFLECT_MAX_DOMAINS": "2"}, clear=False):
            with mock.patch("mimir_v8.reflect._call_llm", _spy):
                r = ReflectionService(self.store).scan()
        self.assertEqual(r["window_days"], 30)
        self.assertEqual(len(calls), 1)
        with contextlib.closing(self.store.connect()) as c:
            row = c.execute("SELECT window_days FROM reflect_runs WHERE run_id=?",
                            (r["run_id"],)).fetchone()
        self.assertEqual(row["window_days"], 30)

    def test_malformed_env_raises_instead_of_silently_defaulting(self):
        """畸形配置必须响亮报错——静默取默认值＝配置错误与没配置不可区分。"""
        for i in range(6):
            _seed(self.store, f"畸形配置 {i}")
        with mock.patch.dict(os.environ, {"MIMIR_REFLECT_WINDOW_DAYS": "七天"},
                             clear=False):
            with self.assertRaises(ValueError):
                ReflectionService(self.store).scan()

    def test_non_dict_payload_degrades_honestly(self):
        for i in range(6):
            _seed(self.store, f"非字典 {i}")
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(["not", "a", "dict"])):
            r = ReflectionService(self.store).scan()
        self.assertEqual(r["insights_created"], 0)
        self.assertEqual(r["llm_failures"], 1)


class TestReflectionLedger(unittest.TestCase):
    """账本三态：completed / failed / 断供欠账可证。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_ledger_counters_recorded(self):
        store = CanonicalStore(Path(self._tmp.name) / "a.db")
        for i in range(6):
            _seed(store, f"计数 {i}")
        payload = {"insights": [{"kind": "pattern", "text": "计数器洞察", "confidence": 0.8}]}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(store).scan()
        with contextlib.closing(store.connect()) as c:
            row = c.execute(
                "SELECT status, window_days, domains_scanned, insights_created,"
                " llm_failures, skipped, completed_at FROM reflect_runs"
                " WHERE run_id=?", (r["run_id"],)).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["window_days"], 7)
        self.assertEqual(row["domains_scanned"], 1)
        self.assertEqual(row["insights_created"], 1)
        self.assertEqual(row["llm_failures"], 0)
        self.assertEqual(row["skipped"], 0)
        self.assertIsNotNone(row["completed_at"])

    def test_ledger_table_is_created_guardedly(self):
        """老库（迁移链已过 V17 的生产库）首次跑新代码必须自愈建表。"""
        store = CanonicalStore(Path(self._tmp.name) / "b.db")
        with contextlib.closing(store.connect()) as c:
            c.execute("DROP TABLE IF EXISTS reflect_runs")
            c.commit()
            self.assertNotIn(
                "reflect_runs",
                [r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()],
            )
        payload = {"insights": []}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            ReflectionService(store).scan()
        with contextlib.closing(store.connect()) as c:
            rows = c.execute("SELECT status FROM reflect_runs").fetchall()
        self.assertEqual([row[0] for row in rows], ["completed"])

    def test_failed_scan_is_recorded_and_reraised(self):
        """主 scan 抛异常：账本记 failed+error_code，异常照常冒泡。"""
        store = CanonicalStore(Path(self._tmp.name) / "c.db")
        with mock.patch("mimir_v8.reflect.ReflectionService._scan_inner",
                        side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ReflectionService(store).scan()
        with contextlib.closing(store.connect()) as c:
            rows = c.execute(
                "SELECT status, error_code FROM reflect_runs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["error_code"], "RuntimeError")

    def test_degraded_close_survives_transaction_outage(self):
        """store 整体不可用（transaction() 抛）时，账本关闭必须降级直写。

        降级路径走 connect() 直写，closing() 的 __exit__ 只关闭不提交——
        漏掉显式 commit() 的 UPDATE 会随连接蒸发。本测试从**另一条连接**
        读回结果，因此「读得到」即是「真提交了」。
        """
        store = CanonicalStore(Path(self._tmp.name) / "e.db")

        class _OutageStore(CanonicalStore):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.transaction_calls = 0

            def transaction(self):
                self.transaction_calls += 1
                if self.transaction_calls >= 2:
                    # 第 1 个事务是 _open_run（必须能写）；其后模拟断供
                    raise RuntimeError("db unavailable (simulated outage)")
                return super().transaction()

        outage = _OutageStore(Path(self._tmp.name) / "e.db")
        payload = {"insights": []}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(outage).scan()
        with contextlib.closing(outage.connect()) as c:
            row = c.execute(
                "SELECT status, completed_at FROM reflect_runs WHERE run_id=?",
                (r["run_id"],)).fetchone()
        self.assertIsNotNone(row, "降级路径也必须关账")
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["completed_at"])

    def test_catchup_counts_unrecovered_failures(self):
        """断供欠账：failed 且其后无 completed 的 run 计入 catchup。"""
        store = CanonicalStore(Path(self._tmp.name) / "d.db")
        with contextlib.closing(store.connect()) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS reflect_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('started','completed','failed')),
                started_at TEXT NOT NULL, completed_at TEXT, error_code TEXT,
                window_days INTEGER, domains_scanned INTEGER DEFAULT 0,
                insights_created INTEGER DEFAULT 0, llm_failures INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0) STRICT""")
            c.execute(
                "INSERT INTO reflect_runs(run_id, status, started_at, window_days)"
                " VALUES('old-failed','failed','2020-01-01T00:00:00+00:00',7)")
            c.commit()
        payload = {"insights": []}
        with mock.patch("mimir_v8.reflect._call_llm", _fake_llm(payload)):
            r = ReflectionService(store).scan()
        self.assertEqual(r["runs"]["catchup"], 1, "欠账必须可证")


if __name__ == "__main__":
    unittest.main()
