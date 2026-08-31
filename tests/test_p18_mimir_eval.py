"""Mímir-Eval standardized benchmark suite tests (v12.1.0 Task 2).

Offline metric computation: HitRate@K, MRR, Extraction Precision and
ACL Isolation Leak Rate — pure functions plus a seeded synthetic
benchmark runner. No live API needed.
"""

from __future__ import annotations

import pytest

from mimir_v8.eval_suite import (
    SyntheticBenchmark,
    compute_acl_leak_rate,
    compute_extraction_precision,
    compute_retrieval_metrics,
)


# ── HitRate@K / MRR ─────────────────────────────────────────

class TestRetrievalMetrics:
    def test_plan_example(self):
        # Exact example from docs/plans/2026-08-23-v12.1.0-implementation.md
        ground_truth = ["fact_1", "fact_2"]
        retrieved = ["fact_3", "fact_1", "fact_4", "fact_2"]
        metrics = compute_retrieval_metrics(ground_truth, retrieved, top_k_list=[1, 3, 5])
        assert metrics["hit_rate@1"] == 0.0
        assert metrics["hit_rate@3"] == 1.0
        assert metrics["mrr"] == pytest.approx(0.5)  # first hit at rank 2 -> 1/2

    def test_perfect_ranking(self):
        metrics = compute_retrieval_metrics(["a", "b"], ["a", "b"], top_k_list=[1, 3, 5])
        assert metrics["hit_rate@1"] == 1.0
        assert metrics["hit_rate@3"] == 1.0
        assert metrics["mrr"] == pytest.approx(1.0)

    def test_no_hits(self):
        metrics = compute_retrieval_metrics(["a"], ["x", "y"], top_k_list=[1, 3, 5])
        assert metrics["hit_rate@1"] == 0.0
        assert metrics["hit_rate@3"] == 0.0
        assert metrics["hit_rate@5"] == 0.0
        assert metrics["mrr"] == 0.0

    def test_empty_retrieved(self):
        metrics = compute_retrieval_metrics(["a"], [], top_k_list=[1, 3, 5])
        assert metrics["hit_rate@1"] == 0.0
        assert metrics["mrr"] == 0.0

    def test_mrr_follows_first_hit_only(self):
        # GT "a" at rank 1 and GT "b" at rank 8: MRR follows the FIRST hit.
        # (Plan pins this semantics: "First hit is at rank 2 -> 1/2".)
        metrics = compute_retrieval_metrics(
            ["a", "b"], ["a", "x", "y", "z", "w", "v", "u", "b"], top_k_list=[1, 3, 5]
        )
        assert metrics["mrr"] == pytest.approx(1.0)

    def test_custom_top_k_list(self):
        metrics = compute_retrieval_metrics(["a"], ["x", "a"], top_k_list=[2])
        assert metrics["hit_rate@2"] == 1.0
        assert "hit_rate@1" not in metrics
        assert metrics["mrr"] == pytest.approx(0.5)

    def test_empty_ground_truth_rejected(self):
        # A benchmark case without ground truth is broken — refuse it.
        with pytest.raises(ValueError):
            compute_retrieval_metrics([], ["a"], top_k_list=[5])


# ── Extraction Precision ─────────────────────────────────────

class TestExtractionPrecision:
    def test_perfect_extraction(self):
        metrics = compute_extraction_precision(
            expected=["fact A", "fact B"], actual=["fact B", "fact A"]
        )
        assert metrics["precision"] == pytest.approx(1.0)
        assert metrics["recall"] == pytest.approx(1.0)
        assert metrics["f1"] == pytest.approx(1.0)

    def test_partial_extraction(self):
        # expected 2, extracted 2, only 1 matches
        metrics = compute_extraction_precision(
            expected=["fact A", "fact B"], actual=["fact A", "noise C"]
        )
        assert metrics["precision"] == pytest.approx(0.5)
        assert metrics["recall"] == pytest.approx(0.5)
        assert metrics["f1"] == pytest.approx(0.5)

    def test_duplicates_counted_once(self):
        metrics = compute_extraction_precision(
            expected=["f1", "f2"], actual=["f1", "f2", "f2"]
        )
        assert metrics["precision"] == pytest.approx(1.0)
        assert metrics["recall"] == pytest.approx(1.0)

    def test_empty_actual(self):
        metrics = compute_extraction_precision(expected=["f1"], actual=[])
        assert metrics["precision"] == 0.0
        assert metrics["recall"] == 0.0
        assert metrics["f1"] == 0.0


# ── ACL Isolation Leak Rate ──────────────────────────────────

class TestAclLeakRate:
    def test_plan_example(self):
        # Exact example from the implementation plan: 2 results, 1 leak -> 0.5
        results = [
            {"agent": "quantmaster", "allowed": True},
            {"agent": "quantmaster", "allowed": False},
        ]
        assert compute_acl_leak_rate(results) == pytest.approx(0.5)

    def test_clean_retrieval(self):
        results = [
            {"agent": "quantmaster", "allowed": True},
            {"agent": "quantmaster", "allowed": True},
        ]
        assert compute_acl_leak_rate(results) == pytest.approx(0.0)

    def test_empty_results_no_leak(self):
        assert compute_acl_leak_rate([]) == pytest.approx(0.0)


# ── Synthetic benchmark runner ───────────────────────────────

class TestSyntheticBenchmark:
    def test_report_shape(self):
        report = SyntheticBenchmark().run()
        assert isinstance(report, dict)
        assert report["provenance"] == "synthetic"
        assert isinstance(report["cases"], list) and len(report["cases"]) > 0
        summary = report["summary"]
        assert summary["n_cases"] == len(report["cases"])
        assert summary["provenance"] == "synthetic"
        reported = {m["metric"] for m in summary["metrics"]}
        assert {"hit_rate@5", "mrr", "extraction_precision", "acl_leak_rate"} <= reported

    def test_metric_values_in_unit_range(self):
        for m in SyntheticBenchmark().run()["summary"]["metrics"]:
            assert 0.0 <= m["value"] <= 1.0

    def test_same_seed_reproducible(self):
        r1 = SyntheticBenchmark(seed=42).run()["summary"]["metrics"]
        r2 = SyntheticBenchmark(seed=42).run()["summary"]["metrics"]
        assert r1 == r2

    def test_different_seed_different_dataset(self):
        r1 = SyntheticBenchmark(seed=42).run()["cases"]
        r2 = SyntheticBenchmark(seed=7).run()["cases"]
        assert r1 != r2

    def test_honest_telemetry_no_production_claim(self):
        # Synthetic numbers must never be presentable as production quality:
        # the report must carry the synthetic provenance stamp at top level.
        report = SyntheticBenchmark().run()
        assert report["provenance"] == "synthetic"
        assert report["generator"].startswith("mimir-eval")
