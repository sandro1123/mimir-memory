# -*- coding: utf-8 -*-
"""1.1.0 Task 6 — LOCOMO / LongMemEval 外部基准 runner。

Roadmap 差距#1（跑分先于一切——数字出来之前架构投资缺背书）。
数据集不进仓（许可面）：runner 吃使用者自备的 JSON 路径；本测试用
**内联微型样本**（2 会话 4 问答）验证解析与计分正确性——手算比对。

计分协议（会话级检索，公开基准同款语义）：
* 语料的每个 session 一条 fact（写路径=store.create_fact，与
  mimir_remember 同一 canonical 面）
* 判据：question → query → top-k 内出现「答案所在 session 的 fact」= 命中
* abstention（无 evidence）案例显式剔除并计数——空集不静默计分

TDD RED → GREEN.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.benchmarks_external import (load_locomo, load_longmemeval,
                                          run_external_benchmark)

# ── 内联微型样本（公开数据集 schema 的最小子集） ──────────────────────

LOCOMO_SAMPLE = [
    {
        "sample_id": "s1",
        "conversation": {
            "speaker_a": "Alice",
            "speaker_b": "Bob",
            "session_1": [
                {"dia_id": "D1:1", "speaker": "Alice", "text": "我上周去了巴黎"},
                {"dia_id": "D1:2", "speaker": "Bob", "text": "真好"},
            ],
            "session_2": [
                {"dia_id": "D2:1", "speaker": "Alice", "text": "我在巴黎进了卢浮宫"},
                {"dia_id": "D2:2", "speaker": "Bob", "text": "卢浮宫很大"},
            ],
        },
        "qa": [
            {"question": "Alice 去了哪个城市？", "answer": "巴黎",
             "evidence": ["D1:1"], "category": 4},
            {"question": "Alice 在巴黎进了什么？", "answer": "卢浮宫",
             "evidence": ["D2:1"], "category": 1},
            {"question": "Alice 的同事是谁？", "answer": None,
             "evidence": [], "category": 5},  # abstention：应剔除计数
        ],
    },
]

LONGMEM_SAMPLE = [
    {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "我喜欢的饮料是什么？",
        "question_date": "2023-05-20",
        "haystack_session_ids": ["ha0", "ha1"],
        "haystack_sessions": [
            [{"role": "user", "content": "我最近喜欢喝抹茶拿铁", "date": "2023-05-01"}],
            [{"role": "user", "content": "今天天气不错", "date": "2023-05-02"}],
        ],
        "answer_session_ids": ["ha0"],
        "answer": "抹茶拿铁",
    },
    {
        "question_id": "q2",
        "question_type": "multi-session",
        "question": "我一共去了几个城市？",
        "question_date": "2023-05-21",
        "haystack_session_ids": ["hb0", "hb1", "hb2"],
        "haystack_sessions": [
            [{"role": "user", "content": "去了北京", "date": "2023-05-03"}],
            [{"role": "user", "content": "去了上海", "date": "2023-05-04"}],
            [{"role": "assistant", "content": "聊天气", "date": "2023-05-05"}],
        ],
        "answer_session_ids": ["hb0", "hb1"],  # 双金标：recall 有意义
        "answer": "两个",
    },
]


def _fake_query(order_table):
    """query_fn 桩：按问题返回固定 fact_id 顺序（手算裁判）。"""
    def fn(question):
        return list(order_table[question])
    return fn


class TestLoaders(unittest.TestCase):

    def test_load_locomo_normalizes_sessions_and_evidence(self):
        corpus = load_locomo(LOCOMO_SAMPLE)
        self.assertEqual(corpus.benchmark, "locomo")
        # 2 sessions；3 qa 里 1 条 abstention 剔除
        self.assertEqual(len(corpus.sessions), 2)
        self.assertEqual(len(corpus.cases), 2)
        self.assertEqual(corpus.n_skipped_no_evidence, 1)
        case = corpus.cases[0]
        self.assertEqual(case.question, "Alice 去了哪个城市？")
        # evidence D1:1 → session_1；fact 身份键确定性可算
        self.assertEqual(case.golden_session_keys,
                         ("locomo:s1:session_1",))
        sess = corpus.sessions[0]
        self.assertEqual(sess.session_key, "locomo:s1:session_1")
        self.assertIn("巴黎", sess.text)

    def test_load_longmemeval_maps_answer_sessions(self):
        corpus = load_longmemeval(LONGMEM_SAMPLE)
        self.assertEqual(corpus.benchmark, "longmemeval")
        self.assertEqual(len(corpus.sessions), 5)  # 2 + 3
        self.assertEqual(len(corpus.cases), 2)
        multi = corpus.cases[1]
        self.assertEqual(sorted(multi.golden_session_keys),
                         sorted(["lme:q2:hb0", "lme:q2:hb1"]))
        self.assertEqual(multi.group, "multi-session")

    def test_malformed_schema_raises(self):
        with self.assertRaises(ValueError) as ctx:
            load_locomo([{"sample_id": "x"}])  # 缺 conversation/qa
        self.assertIn("locomo", str(ctx.exception).lower())
        with self.assertRaises(ValueError):
            load_longmemeval([{"question_id": "q"}])


class TestScoring(unittest.TestCase):

    def _corpus(self):
        return load_longmemeval(LONGMEM_SAMPLE)

    def test_hand_checked_metrics(self):
        corpus = self._corpus()
        # q1: golden=ha0；返回 [ha1, ha0] → rank2
        # q2: golden={hb0,hb1}；返回 [hb1, x, hb0] → hb1@1, hb0@3；recall@3=2/2
        table = {
            "我喜欢的饮料是什么？": ["lme:q1:ha1", "lme:q1:ha0"],
            "我一共去了几个城市？": ["lme:q2:hb1", "junk", "lme:q2:hb0"],
        }
        report = run_external_benchmark(corpus, _fake_query(table),
                                        top_k_list=(1, 3))
        m = report["summary"]["metrics"]
        # hit@1: q1 miss + q2 hit = 1/2 = 0.5；hit@3: 2/2 = 1.0
        self.assertAlmostEqual(m["hit_rate@1"], 0.5)
        self.assertAlmostEqual(m["hit_rate@3"], 1.0)
        # recall 分母=案例 golden 集大小：
        # recall@1: q1 0/1 + q2 1/2 → (0+0.5)/2 = 0.25
        # recall@3: q1 1/1 + q2 2/2 → (1.0+1.0)/2 = 1.0
        self.assertAlmostEqual(m["recall@1"], 0.25)
        self.assertAlmostEqual(m["recall@3"], 1.0)
        # mrr: q1 首命中 rank2 → 0.5；q2 rank1 → 1.0；均值 0.75
        self.assertAlmostEqual(m["mrr"], 0.75)
        self.assertEqual(report["provenance"], "longmemeval")
        self.assertEqual(report["summary"]["n_cases"], 2)

    def test_by_group_breakdown(self):
        corpus = self._corpus()
        table = {
            "我喜欢的饮料是什么？": ["lme:q1:ha0"],
            "我一共去了几个城市？": ["nothing"],
        }
        report = run_external_benchmark(corpus, _fake_query(table),
                                        top_k_list=(10,))
        by = report["by_group"]
        self.assertEqual(by["single-session-user"]["hit_rate@10"], 1.0)
        self.assertEqual(by["multi-session"]["hit_rate@10"], 0.0)

    def test_abstention_never_scored(self):
        corpus = load_locomo(LOCOMO_SAMPLE)
        report = run_external_benchmark(
            corpus, lambda q: [], top_k_list=(10,))
        # 3 qa 里只有 2 条参与计分；n_skipped 显式上报
        self.assertEqual(report["summary"]["n_cases"], 2)
        self.assertEqual(report["summary"]["n_skipped_no_evidence"], 1)

    def test_query_error_degrades_case_not_run(self):
        corpus = self._corpus()
        def flaky(question):
            if question.startswith("我喜欢的"):
                raise RuntimeError("channel exploded")
            return ["lme:q2:hb0", "lme:q2:hb1"]
        report = run_external_benchmark(corpus, flaky, top_k_list=(10,))
        self.assertEqual(report["summary"]["n_query_errors"], 1)
        # 出错案例不计入分母（degraded≠miss——三态纪律）
        self.assertEqual(report["summary"]["n_scored"], 1)
        self.assertAlmostEqual(
            report["summary"]["metrics"]["hit_rate@10"], 1.0)


class TestLoComoFlow(unittest.TestCase):

    def test_locomo_end_to_end_with_stub(self):
        corpus = load_locomo(LOCOMO_SAMPLE)
        table = {
            "Alice 去了哪个城市？": ["locomo:s1:session_2", "locomo:s1:session_1"],
            "Alice 在巴黎进了什么？": ["locomo:s1:session_2"],
        }
        report = run_external_benchmark(corpus, _fake_query(table),
                                        top_k_list=(1, 2))
        self.assertEqual(report["provenance"], "locomo")
        self.assertAlmostEqual(
            report["summary"]["metrics"]["hit_rate@1"], 0.5)
        self.assertAlmostEqual(
            report["summary"]["metrics"]["hit_rate@2"], 1.0)
        self.assertAlmostEqual(
            report["summary"]["metrics"]["mrr"], 0.75)


if __name__ == "__main__":
    unittest.main()
