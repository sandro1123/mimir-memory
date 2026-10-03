#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据集指纹复算器（不联网）。

    python scripts/verify_benchmark_data.py --data path/to/locomo10.json \
                                            [--manifest benchmarks/data_manifest.json]

存在 manifest → 逐样本比对 sha256 与计数，任何不符即非零退出。
不存在 manifest → 生成一份（--write）。

设计纪律（承 `docs/BENCHMARK-PROTOCOL.md`）：
* **数据集不进仓，指纹进仓**——本脚本让 manifest 可离线复算，
  换版本必重新生成，堵住「悄悄换了数据集数字还对得上」这条路。
* **不符要大声拒绝**，不打印 warning 后返回 0。
* 指纹算法（canonical json + sha256）写死在此，避免各处实现漂移。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 逐样本指纹算法：canonical JSON（sort_keys + 不转义非 ASCII）+ sha256。
#: 与生成 manifest 的那段代码必须逐字一致，改这里等于换协议。
def sample_sha256(sample: Any) -> str:
    payload = json.dumps(sample, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, sample in enumerate(raw):
        qa = sample.get("qa") or []
        sessions = sorted(
            key for key in (sample.get("conversation") or {})
            if key.startswith("session_")
        )
        out.append({
            "sample_id": str(sample.get("sample_id", index)),
            "n_sessions": len(sessions),
            "session_keys": sessions,
            "n_qa": len(qa),
            "n_qa_no_evidence": sum(
                1 for q in qa if not (q.get("evidence") or [])),
            "sample_sha256": sample_sha256(sample),
        })
    return out


def build_manifest(raw: list[dict[str, Any]], path: Path,
                   *, source_url: str) -> dict[str, Any]:
    per_sample = summarize(raw)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": path.stem.split(".")[0] or "unknown",
        "source_url": source_url,
        "license_note": "外部数据集，不进仓；仅以 sha256 指纹锁定版本",
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "n_samples": len(raw),
        "n_sessions_total": sum(s["n_sessions"] for s in per_sample),
        "n_qa_total": sum(s["n_qa"] for s in per_sample),
        "n_qa_no_evidence": sum(s["n_qa_no_evidence"] for s in per_sample),
        "samples": per_sample,
    }


def verify(raw: list[dict[str, Any]], path: Path,
           manifest: dict[str, Any]) -> list[str]:
    """返回不符项列表；空列表 = 完全一致。"""
    problems: list[str] = []
    actual_file = file_sha256(path)
    if manifest.get("sha256") != actual_file:
        problems.append(f"file sha256 不符: manifest={manifest.get('sha256')} "
                        f"actual={actual_file}")
    if manifest.get("bytes") != path.stat().st_size:
        problems.append(f"bytes 不符: manifest={manifest.get('bytes')} "
                        f"actual={path.stat().st_size}")
    actual_samples = summarize(raw)
    expected_samples = manifest.get("samples") or []
    if len(actual_samples) != len(expected_samples):
        problems.append(f"样本数不符: manifest={len(expected_samples)} "
                        f"actual={len(actual_samples)}")
    for expect, actual in zip(expected_samples, actual_samples):
        if expect.get("sample_sha256") != actual["sample_sha256"]:
            problems.append(
                f"样本指纹不符 sample_id={actual['sample_id']}: "
                f"manifest={str(expect.get('sample_sha256'))[:16]} "
                f"actual={actual['sample_sha256'][:16]}")
        if expect.get("n_qa") != actual["n_qa"]:
            problems.append(f"样本 {actual['sample_id']} n_qa 不符: "
                            f"{expect.get('n_qa')} vs {actual['n_qa']}")
    for key in ("n_qa_total", "n_qa_no_evidence", "n_sessions_total"):
        expected = manifest.get(key)
        actual = sum(a[key.replace("n_qa_total", "n_qa")
                           .replace("n_qa_no_evidence", "n_qa_no_evidence")
                           .replace("n_sessions_total", "n_sessions")] for a in actual_samples)
        if expected != actual:
            problems.append(f"{key} 不符: manifest={expected} actual={actual}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LoCoMo/LongMemEval 数据集指纹复算与校验")
    parser.add_argument("--data", required=True, help="数据集 JSON 路径")
    parser.add_argument("--manifest", default="benchmarks/data_manifest.json")
    parser.add_argument("--write", action="store_true",
                        help="manifest 不存在或显式要求时生成它")
    parser.add_argument("--source-url", default="", help="生成时写入 manifest 的来源")
    args = parser.parse_args(argv)

    data_path = Path(args.data)
    if not data_path.is_file():
        print(json.dumps({"status": "missing_data", "path": str(data_path)},
                         ensure_ascii=False))
        return 2
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("data") or raw.get("items") or []
    if not isinstance(raw, list) or not raw:
        print(json.dumps({"status": "unparsable", "n": len(raw) if raw else 0},
                         ensure_ascii=False))
        return 2

    manifest_path = Path(args.manifest)
    if args.write or not manifest_path.is_file():
        manifest = build_manifest(raw, data_path,
                                  source_url=args.source_url)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8")
        print(json.dumps({
            "status": "written", "manifest": str(manifest_path),
            "sha256": manifest["sha256"], "n_samples": manifest["n_samples"],
            "n_qa_total": manifest["n_qa_total"],
            "n_qa_no_evidence": manifest["n_qa_no_evidence"],
        }, ensure_ascii=False))
        return 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = verify(raw, data_path, manifest)
    if problems:
        print(json.dumps({"status": "mismatch", "n_problems": len(problems),
                          "problems": problems[:20]}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps({
        "status": "ok", "manifest": str(manifest_path),
        "sha256": manifest.get("sha256"), "n_samples": manifest.get("n_samples"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())