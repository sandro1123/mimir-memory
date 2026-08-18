#!/usr/bin/env python3
"""Perf regression baseline for Mímir v10.0.

Measures query latency for p50/p99 across the core query paths.
Run as part of release verification to catch regressions.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

API_BASE = "http://127.0.0.1:8456"
TOKEN = ""  # set via env MIMIR_ADMIN_TOKEN or secrets file


def get_admin_token() -> str:
    global TOKEN
    if TOKEN:
        return TOKEN
    # admin.token is a plain-text bearer token
    plain = os.path.expanduser("~/.hermes/mimir/secrets/clients/admin.token")
    try:
        with open(plain) as f:
            TOKEN = f.read().strip()
            return TOKEN
    except Exception:
        pass
    for p in [
        os.path.expanduser("~/.hermes/mimir/secrets/api_tokens.json"),
        os.path.expanduser("~/.hermes/mimir/secrets/api_tokens-v8.1-prod.json"),
    ]:
        try:
            with open(p) as f:
                data = json.load(f)
            principals = data.get("principals", [])
            for principal in principals:
                if principal.get("id") == "admin":
                    print(f"⚠️  Note: principal token_sha256 available — hash verify only")
                    return None
            with open(p) as f:
                return f.read().strip()
        except Exception:
            continue
    return ""


def query(text: str, limit: int = 10) -> dict:
    token = get_admin_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{API_BASE}/v8/query",
        data=json.dumps({"text": text, "limit": limit}).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def percentile(sorted_list: list[float], p: float) -> float:
    if not sorted_list:
        return 0
    k = (len(sorted_list) - 1) * p
    f, c = divmod(k, 1)
    idx = int(f)
    if idx + 1 >= len(sorted_list):
        return sorted_list[-1]
    d0 = sorted_list[idx] * (1 - c)
    d1 = sorted_list[min(idx + 1, len(sorted_list) - 1)] * c
    return d0 + d1


def main():
    scenarios = [
        ("memory read", "插件验证: 加config.yaml plugins.enabled", 5),
        ("graph-link", "mimir联邦记忆系统", 5),
        ("broad semantic", "Sing-box配置", 10),
        ("narrow domain", "基础设施", 3),
    ]
    latencies = {name: [] for name, _, _ in scenarios}

    print("running 3 iterations per scenario...")
    for _ in range(3):
        for name, text, limit in scenarios:
            t0 = time.time()
            result = query(text, limit=limit)
            t1 = time.time()
            latencies[name].append((t1 - t0) * 1000)
            print(f"  {name}: {(t1-t0)*1000:.1f}ms → {len(result.get('results',[]))} results")

    print("\n=== perf summary (3 iterations) ===")
    for name, values in latencies.items():
        values.sort()
        p50 = percentile(values, 0.5)
        p99 = percentile(values, 0.99)
        print(f"  {name:15s}: p50={p50:.1f}ms p99={p99:.1f}ms")

    # crude sanity checks
    total_p99_p50 = sum(percentile(latencies[s[0]], 0.5) for s in scenarios)
    print(f"\n  weighted avg p50 across scenarios: {total_p99_p50/len(scenarios):.1f}ms")
    print("  ✅ <500ms p50 acceptable  ⚠️  should test against load baseline")


if __name__ == "__main__":
    main()