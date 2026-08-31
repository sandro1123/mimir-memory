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
benchmark (tests/test_r9_eval.py), not from here.
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Sequence

GENERATOR = "mimir-eval/12.1.0"
PROVENANCE_SYNTHETIC = "synthetic"


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
        pool = [f"{marker}#fact_{i}" for i in range(8)]
        truth = rng.sample(pool, k=2)
        distractors = [f"distractor_{index}_{i}" for i in range(6)]
        # Ground truth lands at a random rank 1..8: exercises the whole
        # hit_rate@K range instead of a single frozen difficulty.
        first_rank = rng.randint(1, 8)
        second_rank = rng.randint(1, 8)
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
            **compute_retrieval_metrics(truth, retrieved, top_k_list=(1, 3, 5)),
            "extraction_precision": compute_extraction_precision(
                expected, actual
            )["precision"],
            "acl_leak_rate": compute_acl_leak_rate(rows),
        }
        return case

    def run(self) -> dict[str, Any]:
        rng = random.Random(self._seed)
        cases = [self._make_case(rng, i) for i in range(self._n_cases)]
        metric_names = ("hit_rate@1", "hit_rate@3", "hit_rate@5", "mrr",
                        "extraction_precision", "acl_leak_rate")
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
