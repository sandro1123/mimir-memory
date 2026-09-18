#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.1.0 一键跑分入口（可复现方法论的载体）。

    python scripts/run_benchmarks.py --golden \
        [--locomo PATH] [--longmemeval PATH] [--out report.json]

三条腿，各司其职：
* --golden      打活服务（默认 http://127.0.0.1:8456，MIMIR_EVAL_API 可
                覆盖）——生产真实质量哨兵，复用 eval_suite.GoldenSetBenchmark
* --locomo/--longmemeval
                **一次性本地库**跑公开基准：语料 session → canonical
                create_fact 写入（session_key 存 legacy_id），QueryKernel
                挂真 FTSProjector 检索（无向量挡位——方法学如实标注在
                report.engine）。绝不往生产库灌基准语料。
* 数据集 JSON 不进仓（许可面），路径自备。

输出：JSON 报告含 版本/时间/engine 元数据 + 各基准指标 + 逐案例面，
发布到 README 的数字必须出自这里（可复现=同数据+同码→同数）。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.benchmarks_external import (load_json_path, load_locomo,  # noqa: E402
                                          load_longmemeval,
                                          run_external_benchmark)
from mimir_v8.eval_suite import (FLOOR_HIT_RATE_3, FLOOR_HIT_RATE_10,  # noqa: E402
                                 GOLDEN_FLOORS, GoldenSetBenchmark,
                                 default_query_fn)
from mimir_v8.schema import (CreateFact, MIMIR_VERSION, SCHEMA_VERSION,  # noqa: E402
                             register_agent)
from mimir_v8.store import CanonicalStore  # noqa: E402


def _ingest_sessions(store: CanonicalStore, corpus) -> dict[str, str]:
    """SessionRecord → canonical facts（legacy_id=session_key）。返回 key→fact_id。"""
    register_agent("benchmark")  # owner 白名单校验：基准写入身份先登记
    key_to_fact: dict[str, str] = {}
    for record in corpus.sessions:
        result = store.create_fact(CreateFact(
            content=record.text, summary=record.text[:200],
            owner_principal="benchmark",
            domain="knowledge", fact_type="reference",
            visibility="all", sensitivity="internal",
            egress_policy="local_only", human_status="confirmed",
            confidence_score=0.5, legacy_id=record.session_key,
        ), actor_principal="benchmark")
        key_to_fact[record.session_key] = result["fact_id"]
    return key_to_fact


def _local_query_fn(store: CanonicalStore, key_to_fact: dict[str, str]):
    """FTS-only 本地检索：question → 有序 session_key 列表。"""
    from mimir_v8.projector import FTSProjector, ProjectorRunner
    from mimir_v8.query import QueryKernel, QueryRequest

    projector = FTSProjector(store.path.parent / "fts.db")
    runner = ProjectorRunner(store, projector)
    while True:  # outbox 排空（p28 同款姿势）
        drained = runner.run_once(limit=200)
        if not drained["processed"] or drained["failed"]:
            break
    kernel = QueryKernel(store, fts=projector)
    fact_to_key = {fid: key for key, fid in key_to_fact.items()}

    def fn(question: str) -> list[str]:
        # depth=deep：基准锚是 reference（L1 型），standard 装配层设计性
        # 不收 L1（v12.2.0 层门）——这里 deep 只是把基准与门语义对齐，
        # 金标腿照旧 standard（测量面不同，report.engine 分别标注）
        response = kernel.search(QueryRequest(
            text=question, principal_id="benchmark", limit=50,
            use_vector=False, use_graph=False, depth="deep"))
        return [fact_to_key[r["fact_id"]] for r in response["results"]
                if r["fact_id"] in fact_to_key]
    return fn


def run_corpus(corpus_name: str, path: str, *, top_k=(1, 3, 5, 10)):
    raw = load_json_path(path)
    corpus = (load_locomo(raw) if corpus_name == "locomo"
              else load_longmemeval(raw))
    with tempfile.TemporaryDirectory() as tmp:
        store = CanonicalStore(Path(tmp) / "bench.db")
        key_to_fact = _ingest_sessions(store, corpus)
        report = run_external_benchmark(corpus,
                                        _local_query_fn(store, key_to_fact),
                                        top_k_list=top_k)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mímir 可复现跑分入口")
    parser.add_argument("--golden", action="store_true",
                        help="金标 24 条打活服务")
    parser.add_argument("--locomo", default=None, help="LOCOMO JSON 路径")
    parser.add_argument("--longmemeval", default=None,
                        help="LongMemEval JSON 路径")
    parser.add_argument("--out", default=None,
                        help="报告输出文件（缺省 stdout）")
    args = parser.parse_args(argv)
    if not (args.golden or args.locomo or args.longmemeval):
        parser.error("至少要选一条腿：--golden / --locomo / --longmemeval")

    report: dict = {
        "mimir_version": MIMIR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": {},
    }
    if args.golden:
        # 活服务面：floor 违规不再让进程吞声——非零退出供流水线阻断
        bench = GoldenSetBenchmark(query_fn=default_query_fn())
        golden = bench.run()
        golden["floors"] = GOLDEN_FLOORS
        golden["floor_breached"] = bool(golden["summary"]["failed_floors"])
        report["results"]["golden"] = golden
    for name, path in (("locomo", args.locomo),
                       ("longmemeval", args.longmemeval)):
        if not path:
            continue
        report["results"][name] = run_corpus(name, path)
    engine = report["results"].get("locomo") or \
        report["results"].get("longmemeval")
    if engine:
        report["engine"] = {"external": "fts-only trigram + RRF "
                                        "(no vector lane)",
                            "golden": "live /v8/query full lanes"}

    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(json.dumps({"status": "ok", "out": args.out},
                         ensure_ascii=False))
    else:
        print(text)
    golden_result = report["results"].get("golden")
    if golden_result and golden_result.get("floor_breached"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
