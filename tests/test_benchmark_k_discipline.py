# -*- coding: utf-8 -*-
"""跑分入口的 K/limit 纪律钉（1.2.0 差距#1 首跑教训的载体）。

首跑 LoCoMo 真相：median first_rank=9，默认 top_k=(1,3,5,10)——近半数命中
落在 rank 10~14，**引擎召回到了却被 K 位切掉**。同一引擎 hit@10=0.46，
宽 K 下显著更高。不把 K 与 limit 变成可测参数，这条教训就会再犯一次：
数字照发，遮住上限，没人知道被遮了多少。

三钉：
* 正向：宽 K 真的产出更宽的指标段，且 limit < max(K) 被拒（不是静默截断）
* 边界：K 列表去重排序、非正整数报错、limit 默认值足够宽
* 反向：拿不到引擎能力的场景必须显式失败，不得输出一个「看起来测过」的数字
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mimir_v8.benchmarks_external import (  # noqa: E402
    BenchmarkCorpus, Case, SessionRecord, run_external_benchmark)


def _corpus(n_sessions: int) -> BenchmarkCorpus:
    return BenchmarkCorpus(
        benchmark="stub",
        sessions=[SessionRecord(f"s{i}", "o", f"text {i}")
                  for i in range(n_sessions)],
        cases=[Case(f"q{i}", (f"s{i}",), "g") for i in range(n_sessions)],
    )


def _load_runner():
    """按路径加载跑分脚本——它不在包内，不能靠 sys.path 撞。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mimir_run_benchmarks_probe",
        REPO_ROOT / "scripts" / "run_benchmarks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTopKDiscipline(unittest.TestCase):
    # ── 正向：宽 K 真的给出更宽的指标段 ──
    def test_wide_k_reports_more_cut_points(self):
        """金标恒在第 26 位——K=10 测 0，宽 K 测 1，差别是引擎能力的全部真相。

        干扰项必须与金标**不重叠**（否则窄 K 也会命中）：干扰取 s20..s39，
        金标是 s0..s19，一律排在干扰之后。
        """
        corpus = _corpus(20)
        query_fn = lambda q: ([f"s{n}" for n in range(20, 40)]  # noqa: E731
                              + [f"s{q[1:]}"])
        narrow = run_external_benchmark(corpus, query_fn,
                                        top_k_list=(1, 3, 5, 10))
        wide = run_external_benchmark(corpus, query_fn,
                                      top_k_list=(1, 10, 20, 25))
        self.assertEqual(narrow["summary"]["metrics"]["hit_rate@10"], 0.0)
        self.assertEqual(wide["summary"]["metrics"]["hit_rate@25"], 1.0)
        # 宽 K 至少不比窄 K 差——截断只会让数字变小，不会变大
        for k in (1, 10):
            self.assertGreaterEqual(wide["summary"]["metrics"][f"hit_rate@{k}"],
                                    narrow["summary"]["metrics"][f"hit_rate@{k}"])

    # ── 边界：K 列表解析 ──
    def test_parse_top_k_dedupes_and_sorts(self):
        module = _load_runner()
        self.assertEqual(module._parse_top_k("10,3,5,3,1"), (1, 3, 5, 10))
        self.assertEqual(module._parse_top_k(" 5 , 5 "), (5,))

    def test_parse_top_k_rejects_garbage(self):
        module = _load_runner()
        for bad in ("abc", "1,0,-3", "", ",", "1.5"):
            with self.subTest(bad=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    module._parse_top_k(bad)

    def test_parse_top_k_rejects_above_engine_ceiling(self):
        """K 超过引擎硬顶必须提前拒——否则 1977 例全 degraded、报告全 None。"""
        module = _load_runner()
        from mimir_v8.query import MAX_QUERY_LIMIT
        with self.assertRaises(argparse.ArgumentTypeError):
            module._parse_top_k(f"1,{MAX_QUERY_LIMIT + 1}")

    # ── 反向：limit 不足必须显式拒绝，不得静默截断出「像测过」的数字 ──
    def test_limit_below_max_k_is_rejected(self):
        """--limit 5 --top-k 1,3,5,10 在旧行为下静默产出 hit@10（永远 0）。"""
        script = REPO_ROOT / "scripts" / "run_benchmarks.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--locomo", "/nonexistent.json",
             "--top-k", "1,3,5,10", "--limit", "5"],
            capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0,
                            "limit < max(top-k) 必须非零退出，不能静默出数")
        self.assertIn("limit", (proc.stderr + proc.stdout).lower())

    def test_limit_above_engine_ceiling_is_rejected(self):
        script = REPO_ROOT / "scripts" / "run_benchmarks.py"
        from mimir_v8.query import MAX_QUERY_LIMIT
        proc = subprocess.run(
            [sys.executable, str(script), "--locomo", "/nonexistent.json",
             "--limit", str(MAX_QUERY_LIMIT + 20)],
            capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)

    def test_engine_block_records_k_and_limit(self):
        """engine 段必须带 top_k 与 limit——否则宽 K 数字会被当成默认 K 数字引用。"""
        src = (REPO_ROOT / "scripts" / "run_benchmarks.py").read_text(
            encoding="utf-8")
        self.assertIn("external_top_k", src)
        self.assertIn("external_candidate_limit", src)


if __name__ == "__main__":
    unittest.main()