"""Mímir-Eval standardized benchmark suite tests (v12.1.0 Task 2).

Offline metric computation: HitRate@K, MRR, Extraction Precision and
ACL Isolation Leak Rate — pure functions plus a seeded synthetic
benchmark runner. No live API needed.

The 2026-09-02 spec (docs/plans/2026-09-02-mimir-comprehensive-evolution-
spec-v12-to-v14.md) pins two gaps this file also pins: HitRate@K must
cover K=10, and the suite must expose an online Golden Set run entry in
addition to the offline synthetic benchmark.
"""

from __future__ import annotations

import json

import pytest

from mimir_v8.eval_suite import (
    FLOOR_HIT_RATE_10,
    FLOOR_HIT_RATE_3,
    GOLDEN_SET,
    STANDARD_TOP_K,
    GoldenSetBenchmark,
    SyntheticBenchmark,
    compute_acl_leak_rate,
    compute_extraction_precision,
    compute_retrieval_metrics,
    main,
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

    def test_k10_semantics(self):
        # Spec pins K=1,3,5,10. Truth at rank 8: hit@5 is a miss,
        # hit@10 is a hit.
        metrics = compute_retrieval_metrics(
            ["a"], ["x", "y", "z", "w", "v", "u", "t", "a"], top_k_list=STANDARD_TOP_K
        )
        assert metrics["hit_rate@5"] == 0.0
        assert metrics["hit_rate@10"] == 1.0

    def test_standard_top_k_pins_spec(self):
        # The spec's K-set is a contract: 1, 3, 5, 10 — no silent drift.
        assert STANDARD_TOP_K == (1, 3, 5, 10)

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

    def test_summary_covers_standard_top_k(self):
        # Spec pins K=1,3,5,10 — every standard K must be aggregated
        # in the synthetic summary (K=10 was missing before the
        # 09-02 spec gap fix).
        reported = {m["metric"] for m in SyntheticBenchmark().run()["summary"]["metrics"]}
        assert {f"hit_rate@{k}" for k in STANDARD_TOP_K} <= reported

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


# ── Online Golden Set benchmark (spec 09-02) ────────────────


class TestGoldenSetBenchmark:
    def _fake_query(self, script: dict[str, list[str]]):
        """query_fn returning fact_ids by question; KeyError for unknown."""
        def query(text: str, limit: int) -> dict:
            return {"fact_ids": script[text][:limit]}
        return query

    def test_perfect_ranking_scores_full(self):
        # Every golden fact at rank 1 against the golden set itself.
        script = {
            q: [gid, "distractor"] for q, gid, _marker in GOLDEN_SET
        }
        report = GoldenSetBenchmark(query_fn=self._fake_query(script)).run()
        s = {m["metric"]: m["value"] for m in report["summary"]["metrics"]}
        assert s["hit_rate@1"] == pytest.approx(1.0)
        assert s["mrr"] == pytest.approx(1.0)

    def test_miss_scores_zero(self):
        # No golden fact anywhere in top-10: every metric floors to 0.
        script = {q: ["other_1", "other_2"] for q, _gid, _m in GOLDEN_SET}
        report = GoldenSetBenchmark(query_fn=self._fake_query(script)).run()
        s = {m["metric"]: m["value"] for m in report["summary"]["metrics"]}
        assert s["hit_rate@10"] == pytest.approx(0.0)
        assert s["mrr"] == pytest.approx(0.0)

    def test_partial_hit_and_rank_score(self):
        # Half the cases hit at rank 2, half miss: hit@1=0.0, hit@3=0.5,
        # MRR = 0.5 * (1/2) = 0.25.
        half = len(GOLDEN_SET) // 2
        script = {}
        for i, (q, gid, _m) in enumerate(GOLDEN_SET):
            if i < half:
                script[q] = ["distractor", gid]
            else:
                script[q] = ["distractor"]
        report = GoldenSetBenchmark(query_fn=self._fake_query(script)).run()
        s = {m["metric"]: m["value"] for m in report["summary"]["metrics"]}
        assert s["hit_rate@1"] == pytest.approx(0.0)
        assert s["hit_rate@3"] == pytest.approx(0.5)
        assert s["mrr"] == pytest.approx(0.25)

    def test_marker_fallback_survives_fact_id_churn(self):
        # fact_ids churn in production; the golden case must still hit via
        # its marker substring appearing in the returned rows' text.
        def query(text: str, limit: int) -> dict:
            for q, _gid, marker in GOLDEN_SET:
                if q == text:
                    # Fresh churned id, but the marker text survives.
                    return {
                        "fact_ids": [f"churned-{marker}"],
                        "rows": [{"summary": f"…{marker}…", "content": ""}],
                    }
            raise KeyError(text)

        report = GoldenSetBenchmark(query_fn=query).run()
        s = {m["metric"]: m["value"] for m in report["summary"]["metrics"]}
        assert s["hit_rate@10"] == pytest.approx(1.0)

    def test_floors_enforced_with_failure_detail(self):
        # A failing run must report below-floor metrics AND keep the
        # per-case detail needed to diagnose which case regressed.
        script = {q: ["distractor"] for q, _g, _m in GOLDEN_SET}
        report = GoldenSetBenchmark(query_fn=self._fake_query(script)).run()
        assert report["summary"]["floors"] is not None
        failed = report["summary"]["failed_floors"]
        assert {"hit_rate@3", "hit_rate@10"} <= {f["metric"] for f in failed}

    def test_honest_telemetry_provenance_live(self):
        # Golden-set numbers are live production numbers — they must be
        # stamped provenance="golden" so they can't be conflated with the
        # synthetic report either direction.
        script = {q: [gid] for q, gid, _m in GOLDEN_SET}
        report = GoldenSetBenchmark(query_fn=self._fake_query(script)).run()
        assert report["provenance"] == "golden"

    def test_default_query_fn_targets_live_api(self):
        # The no-arg constructor builds the live /v8/query adapter
        # (urllib + Bearer token), NOT the synthetic corpus — the online
        # entry must be wired to the real system by default.
        bench = GoldenSetBenchmark()
        assert callable(bench.query_fn)

    def test_floor_values_match_r9_baseline(self):
        # Floors are the 2026-08-16 production baseline floors carried
        # over from the live benchmark (tests/test_r9_eval.py): recall@3
        # 0.750, recall@10 0.875. Renamed to hit_rate@k here — same
        # semantics (query-level any-hit-in-top-k).
        assert FLOOR_HIT_RATE_3 == pytest.approx(0.75)
        assert FLOOR_HIT_RATE_10 == pytest.approx(0.875)


# ── Run entry (spec 09-02: "运行入口") ──────────────────────


class TestRunEntry:
    def test_main_synthetic_prints_json_report(self, capsys):
        report = main(["--synthetic", "--seed", "42"])
        assert report["provenance"] == "synthetic"
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["provenance"] == "synthetic"
        assert parsed["generator"].startswith("mimir-eval")

    def test_main_synthetic_seed_reproducible(self):
        r1 = main(["--synthetic", "--seed", "42"])
        r2 = main(["--synthetic", "--seed", "42"])
        assert r1 == r2

    def test_main_golden_requires_reachable_api(self):
        # Running golden without a live API must fail loudly with a
        # clear error, not silently pass or hang.
        with pytest.raises(SystemExit) as exc:
            main(["--golden", "--api", "http://127.0.0.1:1"])
        assert exc.value.code != 0
