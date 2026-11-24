#!/usr/bin/env python3
"""Governance model dry-run eval (read-only).

Samples already-decided candidates from the production DB (read-only), sends each
through the *real* governance EVALUATION_PROMPT to several candidate models, and
records latency, prompt/completion tokens, JSON validity and agreement with the
final status. Never writes to the DB. Skips anything not 'internal'/'public'
sensitivity so nothing sensitive leaves the box.

Usage:
  MIMIR_DATA_DIR=<data_dir> MIMIR_EVAL_MAGPIE_URL=<magpie>/v1 \
  PYTHONPATH=. python3 scripts/governance_model_eval.py --n 20 --out /path/result.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sqlite3
import statistics
import time
import urllib.request
from pathlib import Path

from mimir_v8.governance import EVALUATION_PROMPT

DB = str(Path(os.environ.get("MIMIR_DATA_DIR", ".")) / "canonical.db")
MAGPIE_URL = os.environ.get("MIMIR_EVAL_MAGPIE_URL", "http://127.0.0.1:3430/v1")
ROUTER_URL = os.environ.get("MIMIR_EVAL_ROUTER_URL", "http://127.0.0.1:20129/v1")


def _env_value(path: Path, name: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


MAGPIE_KEY = os.environ.get("MAGPIE_API_KEY") or _env_value(Path.home() / ".hermes/.env", "MAGPIE_API_KEY")
ROUTER_KEY = _env_value(Path.home() / ".hermes/mimir/secrets/evaluator.env", "MIMIR_EVALUATOR_API_KEY")

MODELS = [
    # (label, base_url, key, model_id)
    ("baseline:10router/cbcn-deepseek-v4.1-flash", ROUTER_URL, ROUTER_KEY, "cbcn/deepseek-v4.1-flash"),
    ("magpie:our-free/deepseek-v4.1-flash-free", MAGPIE_URL, MAGPIE_KEY, "our-free-model/deepseek-v4.1-flash-free"),
    ("magpie:our-free/mimo-v2.6-flash-free", MAGPIE_URL, MAGPIE_KEY, "our-free-model/mimo-v2.6-flash-free"),
    ("magpie:auto/deepseek-v4.1-flash-free", MAGPIE_URL, MAGPIE_KEY, "auto/deepseek-v4.1-flash-free"),
    ("magpie:group/glm-5.3-flash", MAGPIE_URL, MAGPIE_KEY, "group/glm-5.3-flash"),
    ("magpie:deepaicode/deepseek-v4.1-flash", MAGPIE_URL, MAGPIE_KEY, "deepaicode/deepseek-v4.1-flash"),
    ("magpie:deepaicode/glm-5.3-flash", MAGPIE_URL, MAGPIE_KEY, "deepaicode/glm-5.3-flash"),
]


def sample(n: int) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    half = max(1, n // 2)
    rows = []
    for statuses, k in ((("committed",), half), (("rejected", "auto_rejected"), n - half)):
        q = (
            "SELECT candidate_id, content, summary, status FROM candidate_facts "
            f"WHERE status IN ({','.join('?' * len(statuses))}) "
            "AND COALESCE(proposed_sensitivity,'internal') IN ('internal','public') "
            "ORDER BY created_at DESC LIMIT ?"
        )
        rows += [dict(r) for r in conn.execute(q, (*statuses, k))]
    conn.close()
    return rows


def call(base: str, key: str, model: str, prompt: str, timeout: int = 45) -> dict:
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "max_tokens": 1500}
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        dt = time.monotonic() - t0
        text = body["choices"][0]["message"].get("content") or ""
        usage = body.get("usage") or {}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = None
        if m:
            try:
                parsed = json.loads(m.group())
            except ValueError:
                parsed = None
        return {"ok": parsed is not None, "latency_s": round(dt, 2), "parsed": parsed,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "error": None if parsed is not None else "no/invalid JSON"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_s": round(time.monotonic() - t0, 2), "parsed": None,
                "prompt_tokens": None, "completion_tokens": None,
                "error": f"{type(exc).__name__}: {exc}"[:160]}


def verdict(parsed: dict | None) -> str | None:
    if not parsed:
        return None
    if parsed.get("is_noise") or parsed.get("is_valuable") is False:
        return "reject"
    if parsed.get("is_valuable"):
        return "keep"
    return None


def run_model(spec, items):
    label, base, key, model = spec
    if not key:
        return label, {"skipped": "no api key"}
    out = []
    for it in items:
        content = (it["content"] or it["summary"] or "")[:2000]
        r = call(base, key, model, EVALUATION_PROMPT.format(content=content))
        truth = "keep" if it["status"] == "committed" else "reject"
        r["agree"] = (verdict(r["parsed"]) == truth) if r["ok"] else None
        r.pop("parsed", None)
        out.append(r)
    ok = [r for r in out if r["ok"]]
    lat = [r["latency_s"] for r in ok]
    pt = [r["prompt_tokens"] for r in ok if r["prompt_tokens"]]
    agree = [r["agree"] for r in ok if r["agree"] is not None]
    return label, {
        "n": len(out), "json_ok": len(ok),
        "agree_rate": round(sum(agree) / len(agree), 2) if agree else None,
        "latency_p50": round(statistics.median(lat), 2) if lat else None,
        "latency_max": max(lat) if lat else None,
        "prompt_tokens_median": int(statistics.median(pt)) if pt else None,
        "errors": sorted({r["error"] for r in out if r["error"]})[:4],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    items = sample(args.n)
    with cf.ThreadPoolExecutor(max_workers=len(MODELS)) as ex:
        results = dict(ex.map(lambda s: run_model(s, items), MODELS))
    report = {"sampled": len(items),
              "label_mix": {s: sum(1 for i in items if i["status"] == s) for s in {i["status"] for i in items}},
              "prompt_chars": len(EVALUATION_PROMPT), "results": results}
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
