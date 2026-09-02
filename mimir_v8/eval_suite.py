"""Mímir-Eval standardized benchmark suite (v12.1.0).

Offline, dependency-free metric layer + a seeded synthetic benchmark
runner. The pure functions define the metric semantics pinned by
docs/plans/2026-08-23-v12.1.0-implementation.md:

- ``hit_rate@K`` is query-level: 1.0 when ANY ground-truth fact appears
  in the top-K retrieved, else 0.0 (same family as the live golden-set
  recall@K in tests/test_r9_eval.py).
- ``mrr`` follows the FIRST ground-truth hit only: 1/rank, 0.0 on miss
  (plan pins this: "First hit is at rank 2 -> 1/2").
- extraction precision/recall/F1 use set semantics (duplicates count once).
- ACL leak rate is the fraction of retrieved rows an agent was NOT
  allowed to see — production floor for this metric is 0.0, any nonzero
  value is a security regression, not a quality trade-off.

Honest telemetry: synthetic runs are stamped ``provenance="synthetic"``
at both report and summary level, so synthetic numbers can never be
presented as production quality. Real floors come from the live
golden-set benchmark (``GoldenSetBenchmark``, provenance ``"golden"``)
— the 2026-09-02 spec pins HitRate@K over K = 1, 3, 5, 10 and an
online Golden Set run entry alongside the offline synthetic one.
tests/test_r9_eval.py keeps its live regression run but imports the
golden set from here so the two can never drift apart.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

GENERATOR = "mimir-eval/12.1.0"
PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_GOLDEN = "golden"

#: The 09-02 spec pins HitRate@K over exactly K = 1, 3, 5, 10. Both
#: benchmark runners aggregate this set — pinning it here keeps the
#: synthetic runner from drifting below the spec (it stopped at 5).
STANDARD_TOP_K: tuple[int, ...] = (1, 3, 5, 10)

#: Live-benchmark regression floors, carried from the 2026-08-16
#: production baseline (tests/test_r9_eval.py): recall@3 = 0.750 and
#: recall@10 = 0.875 measured with trigram FTS + weighted RRF on N100.
#: Query-level any-hit semantics — identical to hit_rate@k, so the
#: values carry over unchanged under the renamed metric.
FLOOR_HIT_RATE_3 = 0.750
FLOOR_HIT_RATE_10 = 0.875

DEFAULT_EVAL_API = os.environ.get("MIMIR_EVAL_API", "http://127.0.0.1:8456")


# ── Pure metric functions ───────────────────────────────────


def compute_retrieval_metrics(
    ground_truth: Sequence[str],
    retrieved: Sequence[str],
    top_k_list: Iterable[int],
) -> dict[str, float]:
    """HitRate@K + MRR for one query case.

    Raises ValueError on empty ground truth — a benchmark case without
    ground truth is broken and must be refused, not scored as a silent 0.
    """
    truth = list(ground_truth)
    if not truth:
        raise ValueError("benchmark case with empty ground_truth is invalid")
    hits = [rank for rank, item in enumerate(retrieved, start=1) if item in set(truth)]
    metrics: dict[str, float] = {}
    for k in top_k_list:
        metrics[f"hit_rate@{k}"] = 1.0 if any(rank <= k for rank in hits) else 0.0
    metrics["mrr"] = 1.0 / min(hits) if hits else 0.0
    return metrics


def compute_extraction_precision(
    expected: Sequence[str],
    actual: Sequence[str],
) -> dict[str, float]:
    """Extraction precision/recall/F1 with set semantics (duplicates once)."""
    expected_set = set(expected)
    actual_set = set(actual)
    matches = expected_set & actual_set
    precision = len(matches) / len(actual_set) if actual_set else 0.0
    recall = len(matches) / len(expected_set) if expected_set else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall > 0
        else 0.0
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def compute_acl_leak_rate(results: Sequence[dict[str, Any]]) -> float:
    """Fraction of retrieved rows the calling agent was not allowed to see.

    Zero results is an empty probe, not a clean pass — but there is
    nothing to leak, so 0.0 is the honest answer.
    """
    if not results:
        return 0.0
    leaks = sum(1 for row in results if not row.get("allowed", True))
    return leaks / len(results)


# ── Synthetic benchmark runner ───────────────────────────────

#: Bilingual (CJK + latin) synthetic corpus so the benchmark exercises the
#: same trigram-FTS / embedding reality the production retrieval serves.
_TOPICS = [
    ("N100 内存过载怎么处理", "N100 内存过载"),
    ("Mentor 的职责是什么", "Mentor 职责"),
    ("obsidian 笔记库重构", "obsidian 重构"),
    ("Sing-box 免费节点恢复", "singbox 节点"),
    ("dashboard 端口编排", "dashboard 端口"),
    ("agent 记忆隔离风险", "记忆隔离风险"),
    ("RSS 采集管道调度", "RSS 调度"),
    ("BGE-M3 离线初始化", "bge-m3 初始化"),
    ("walk_forward 首跑验收", "walk_forward 验收"),
    ("flock 锁窗治理", "flock 锁窗"),
    ("litestream 增量备份", "litestream 备份"),
    ("HITL 审批台账", "hitl 台账"),
]

_AGENT_PAIRS = [
    ("quantmaster", "mentor"),
    ("jarvis", "heimdallr"),
    ("mentor", "quantmaster"),
    ("heimdallr", "jarvis"),
]


class SyntheticBenchmark:
    """Seeded offline benchmark over the pure metric functions.

    Produces a deterministic per-seed case corpus (retrieval rankings,
    extraction sets, ACL probe rows) and aggregates per-case metrics into
    a summary. ``seed=None`` draws a fresh corpus each run; a fixed seed
    reproduces it exactly — the property the reproducibility tests pin.
    """

    def __init__(self, seed: int | None = None, n_cases: int = 12) -> None:
        self._seed = seed
        self._n_cases = min(max(n_cases, 1), len(_TOPICS))

    def _make_case(self, rng: random.Random, index: int) -> dict[str, Any]:
        question, marker = _TOPICS[index]
        # Pool and ranking depth 12 (not 8): K=10 must be non-trivial,
        # i.e. there must be rankings where truth sits beyond rank 10.
        pool = [f"{marker}#fact_{i}" for i in range(8)]
        truth = rng.sample(pool, k=2)
        distractors = [f"distractor_{index}_{i}" for i in range(10)]
        # Ground truth lands at a random rank 1..12: exercises the whole
        # hit_rate@K range including the K=10 boundary.
        first_rank = rng.randint(1, 12)
        second_rank = rng.randint(1, 12)
        retrieved = list(distractors)
        retrieved.insert(min(first_rank - 1, len(retrieved)), truth[0])
        retrieved.insert(min(second_rank - 1, len(retrieved)), truth[1])

        expected = rng.sample(pool, k=3)
        actual = expected[: rng.randint(1, 3)] + rng.sample(distractors, k=rng.randint(0, 2))

        agent, foreign_agent = _AGENT_PAIRS[index % len(_AGENT_PAIRS)]
        rows = [{"agent": agent, "allowed": True} for _ in range(rng.randint(1, 4))]
        if rng.random() < 0.25:  # occasional isolation probe row
            rows.append({"agent": agent, "allowed": False, "owner": foreign_agent})

        case = {
            "question": question,
            "ground_truth": truth,
            "retrieved": retrieved,
            "extraction": {"expected": expected, "actual": actual},
            "acl_results": rows,
        }
        case["metrics"] = {
            **compute_retrieval_metrics(truth, retrieved, top_k_list=STANDARD_TOP_K),
            "extraction_precision": compute_extraction_precision(
                expected, actual
            )["precision"],
            "acl_leak_rate": compute_acl_leak_rate(rows),
        }
        return case

    def run(self) -> dict[str, Any]:
        rng = random.Random(self._seed)
        cases = [self._make_case(rng, i) for i in range(self._n_cases)]
        metric_names = (
            *(f"hit_rate@{k}" for k in STANDARD_TOP_K),
            "mrr",
            "extraction_precision",
            "acl_leak_rate",
        )
        metrics = [
            {
                "metric": name,
                "value": sum(c["metrics"][name] for c in cases) / len(cases),
            }
            for name in metric_names
        ]
        return {
            "generator": GENERATOR,
            "provenance": PROVENANCE_SYNTHETIC,
            "cases": cases,
            "summary": {
                "n_cases": len(cases),
                "provenance": PROVENANCE_SYNTHETIC,
                "metrics": metrics,
                # Synthetic corpora are provisional by construction: real
                # floors live in the live golden-set benchmark (test_r9).
                "notes": "synthetic corpus — provisional values, not production quality",
            },
        }


# ── Online Golden Set benchmark ──────────────────────────────

#: (question, golden_fact_id, marker_substring) — the live golden set,
#: carried from tests/test_r9_eval.py (2026-08-16 baseline) so the CLI
#: benchmark and the regression test share one source of truth. Marker
#: substrings let a case survive fact_id churn: ids churn in
#: production, distinctive content doesn't.
GOLDEN_SET: tuple[tuple[str, str, str], ...] = (
    ("Mentor 的职责是什么", "dad7aea2-f7e9-4b86-b9ea-2e3591a3bb9f", "运维职责"),
    ("N100 内存过载怎么处理", "4389e49d-5c2b-4d18-a45a-e234de679709", "N100 内存过载"),
    ("记忆系统有哪些常见故障", "4bddde4a-4c46-4370-a84f-5a7d0e1bd442", "常见故障"),
    ("早间新闻要怎么呈现", "2de24c79-a228-442d-8d68-a0297f41bc75", "早间新闻"),
    ("回复卡片 header 改成什么", "57f4c028-fa14-4aa4-b6c6-c7a449194280", "Heimdallr-EX"),
    ("多个 agent 共享记忆池有什么风险", "789eb5c9-b45c-445e-b48b-10320bc5bb74", "共享记忆池"),
    ("obsidian 笔记库乱了怎么重构", "7fce0a72-be1e-4fe1-b0fd-cc4b00897250", "obsidian笔记库"),
    ("让所有 agent 都部署记忆系统", "8e2e6a41-087d-4dfa-970f-c05f97d9ba3c", "部署Mimir"),
)

#: Metric → floor for the golden run. Same semantics as the r9 baseline
#: floors (query-level any-hit-in-top-k), pinned in one place so the
#: regression test and the CLI can never disagree.
GOLDEN_FLOORS: dict[str, float] = {
    "hit_rate@3": FLOOR_HIT_RATE_3,
    "hit_rate@10": FLOOR_HIT_RATE_10,
}


def default_query_fn(
    api: str = DEFAULT_EVAL_API,
    token: str | None = None,
) -> Callable[[str, int], dict[str, Any]]:
    """Build the live /v8/query adapter (urllib + optional Bearer token).

    Same wire contract as tests/test_r9_eval.py: POST {text, limit},
    read results[].fact_id plus summary/content for marker fallback.
    """
    if token is None:
        token_path = Path.home() / ".hermes/mimir/secrets/clients/admin.token"
        token = token_path.read_text().strip() if token_path.exists() else ""

    def query(text: str, limit: int) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps({"text": text, "limit": limit}).encode()
        request = urllib.request.Request(
            f"{api.rstrip('/')}/v8/query", data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        rows = data.get("results", [])
        return {
            "fact_ids": [r.get("fact_id") for r in rows],
            "rows": rows,
        }

    return query


class GoldenSetBenchmark:
    """Benchmark the live system against the pinned golden set.

    ``query_fn(text, limit) -> {"fact_ids": [...], "rows": [...]}`` is
    injectable so the scoring logic is testable offline; the default
    constructor wires the real /v8/query adapter. A case hits when the
    golden fact_id surfaces in the top-K retrieved — or when its marker
    substring survives in a returned row's text, which keeps the
    benchmark honest across fact_id churn.
    """

    def __init__(self, query_fn: Callable[[str, int], dict[str, Any]] | None = None) -> None:
        if query_fn is None:
            query_fn = default_query_fn()
        self.query_fn = query_fn

    def _rank_of(
        self, question: str, golden_id: str, marker: str, limit: int
    ) -> int | None:
        response = self.query_fn(question, limit)
        fact_ids = [fid for fid in response.get("fact_ids", []) if fid]
        if golden_id in fact_ids:
            return fact_ids.index(golden_id) + 1
        for i, row in enumerate(response.get("rows", []), start=1):
            text = (row.get("summary") or row.get("content") or "")
            if marker.lower() in text.lower() or marker in text:
                return i
        return None

    def run(self, top_k_list: Iterable[int] = STANDARD_TOP_K) -> dict[str, Any]:
        top_k_list = list(top_k_list)
        cases = []
        for question, golden_id, marker in GOLDEN_SET:
            rank = self._rank_of(question, golden_id, marker, limit=max(top_k_list))
            cases.append({"question": question, "rank": rank})

        metrics: dict[str, float] = {}
        for k in top_k_list:
            metrics[f"hit_rate@{k}"] = (
                sum(1 for c in cases if c["rank"] is not None and c["rank"] <= k) / len(cases)
            )
        metrics["mrr"] = (
            sum(1.0 / c["rank"] for c in cases if c["rank"]) / len(cases)
        )

        summary_metrics = [
            {"metric": name, "value": value} for name, value in metrics.items()
        ]
        failed_floors = [
            {"metric": name, "value": metrics[name], "floor": floor}
            for name, floor in GOLDEN_FLOORS.items()
            if metrics[name] < floor
        ]
        return {
            "generator": GENERATOR,
            "provenance": PROVENANCE_GOLDEN,
            "cases": cases,
            "summary": {
                "n_cases": len(cases),
                "provenance": PROVENANCE_GOLDEN,
                "metrics": summary_metrics,
                "floors": GOLDEN_FLOORS,
                "failed_floors": failed_floors,
                "notes": "live golden-set run — production quality numbers",
            },
        }


# ── Run entry (CLI) ──────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mimir-eval",
        description=(
            "Mímir-Eval benchmark runner: offline synthetic corpus or "
            "online golden-set run against the live /v8/query API."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--synthetic", action="store_true",
        help="offline seeded synthetic benchmark (no live API needed)",
    )
    mode.add_argument(
        "--golden", action="store_true",
        help="online golden-set benchmark against the live Mímir API",
    )
    parser.add_argument("--seed", type=int, default=None,
                        help="synthetic corpus seed (reproducible runs)")
    parser.add_argument("--api", default=DEFAULT_EVAL_API,
                        help="Mímir API base URL for --golden")
    parser.add_argument("--token", default=None,
                        help="Bearer token (default: admin.token file or env)")
    return parser


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    """CLI entry: run a benchmark, print the JSON report, return it.

    Golden mode exits nonzero when the API is unreachable or the run
    lands below any floor — a failing regression must be loud, not a
    silently green exit.
    """
    args = _build_arg_parser().parse_args(argv)
    if args.synthetic:
        report = SyntheticBenchmark(seed=args.seed).run()
    else:
        bench = GoldenSetBenchmark(
            query_fn=default_query_fn(api=args.api, token=args.token)
        )
        try:
            report = bench.run()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(
                json.dumps({"error": f"golden benchmark unreachable: {exc}"}),
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
        if report["summary"]["failed_floors"]:
            print(
                json.dumps(
                    {
                        "error": "golden benchmark below floor",
                        "failed_floors": report["summary"]["failed_floors"],
                    }
                ),
                file=sys.stderr,
            )
            raise SystemExit(3)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()
