"""Mímir v12 retrieval eval baseline (P1-1).

Self-built golden-query benchmark executed against the live production API
(skipped when unreachable). Establishes regression floors for:

- recall@3 / recall@10: known fact must surface within top-K
- p50 latency: interactive recall budget

Run:  pytest tests/test_r9_eval.py -q
      MIMIR_EVAL_API=http://127.0.0.1:8456 MIMIR_EVAL_TOKEN=<admin token>

The golden set is derived from production facts (fixed fact_ids). Each case
maps a natural-language user question to the fact it should retrieve.
Marker strings double as soft checks when fact_ids churn.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = os.environ.get("MIMIR_EVAL_API", "http://127.0.0.1:8456")
TOKEN = os.environ.get(
    "MIMIR_EVAL_TOKEN",
    (Path.home() / ".hermes/mimir/secrets/clients/admin.token").read_text().strip()
    if (Path.home() / ".hermes/mimir/secrets/clients/admin.token").exists() else "",
)

#: (question, golden_fact_id, marker_substring)
GOLDEN_SET = [
    ("Mentor 的职责是什么", "dad7aea2-f7e9-4b86-b9ea-2e3591a3bb9f", "运维职责"),
    ("N100 内存过载怎么处理", "4389e49d-5c2b-4d18-a45a-e234de679709", "N100 内存过载"),
    ("记忆系统有哪些常见故障", "4bddde4a-4c46-4370-a84f-5a7d0e1bd442", "常见故障"),
    ("早间新闻要怎么呈现", "2de24c79-a228-442d-8d68-a0297f41bc75", "早间新闻"),
    ("回复卡片 header 改成什么", "57f4c028-fa14-4aa4-b6c6-c7a449194280", "Heimdallr-EX"),
    ("多个 agent 共享记忆池有什么风险", "789eb5c9-b45c-445e-b48b-10320bc5bb74", "共享记忆池"),
    ("obsidian 笔记库乱了怎么重构", "7fce0a72-be1e-4fe1-b0fd-cc4b00897250", "obsidian笔记库"),
    ("让所有 agent 都部署记忆系统", "8e2e6a41-087d-4dfa-970f-c05f97d9ba3c", "部署Mimir"),
]

#: Regression floors. 2026-08-16 baseline after trigram FTS + weighted RRF +
#: CJK trigram-fragment expansion measured recall@3 = 100%, recall@10 = 100%,
#: p50 ≈ 223ms on N100 CPU. Floors leave headroom for production data churn
#: while still failing on a real degradation.
FLOOR_RECALL_3 = 0.750
FLOOR_RECALL_10 = 0.875
FLOOR_P50_MS = 1500.0


def _query(text: str, limit: int) -> tuple[list[dict], float]:
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps({"text": text, "limit": limit}).encode()
    request = urllib.request.Request(f"{API}/v8/query", data=body,
                                     headers=headers, method="POST")
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read())
    elapsed_ms = (time.monotonic() - start) * 1000
    return data.get("results", []), elapsed_ms


def _api_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


@unittest.skipUnless(_api_reachable(), "Mímir API not reachable — eval is a live-system benchmark")
class RetrievalEvalBaseline(unittest.TestCase):

    def test_recall_against_golden_set(self):
        latencies = []
        per_case = []
        for question, golden_id, marker in GOLDEN_SET:
            results, elapsed_ms = _query(question, limit=10)
            latencies.append(elapsed_ms)
            ids = [r["fact_id"] for r in results]
            rank = ids.index(golden_id) + 1 if golden_id in ids else None
            if rank is None:  # allow content drift: fall back to marker match
                for i, r in enumerate(results, 1):
                    text = (r.get("summary") or r.get("content") or "")
                    if marker.lower() in text.lower() or marker in text:
                        rank = i
                        break
            per_case.append((question, rank))

        hits_at_3 = sum(1 for _, rank in per_case if rank and rank <= 3)
        hits_at_10 = sum(1 for _, rank in per_case if rank and rank <= 10)
        total = len(per_case)
        recall_3 = hits_at_3 / total
        recall_10 = hits_at_10 / total
        p50_ms = statistics.median(latencies)

        report = "\n".join(
            f"  {'HIT' if rank else 'MISS'} rank={rank or '-':>2} | {q}"
            for q, rank in per_case
        )
        summary = (
            f"\nrecall@3  = {hits_at_3}/{total} ({recall_3:.1%}) floor {FLOOR_RECALL_3:.1%}\n"
            f"recall@10 = {hits_at_10}/{total} ({recall_10:.1%}) floor {FLOOR_RECALL_10:.1%}\n"
            f"p50 = {p50_ms:.0f}ms floor {FLOOR_P50_MS:.0f}ms\n{report}"
        )

        self.assertGreaterEqual(recall_3, FLOOR_RECALL_3, f"recall@3 below floor{summary}")
        self.assertGreaterEqual(recall_10, FLOOR_RECALL_10, f"recall@10 below floor{summary}")
        self.assertLessEqual(p50_ms, FLOOR_P50_MS, f"p50 latency over budget{summary}")


if __name__ == "__main__":
    unittest.main()
