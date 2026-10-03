#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.1.0 一键跑分入口（可复现方法论的载体）。

    python scripts/run_benchmarks.py --golden \
        [--locomo PATH] [--longmemeval PATH] [--vector] [--out report.json]

三条腿，各司其职：
* --golden      打活服务（默认 http://127.0.0.1:8456，MIMIR_EVAL_API 可
                覆盖）——生产真实质量哨兵，复用 eval_suite.GoldenSetBenchmark
* --locomo/--longmemeval
                **一次性本地库**跑公开基准：语料 session → canonical
                create_fact 写入（session_key 存 legacy_id），QueryKernel
                挂真 FTSProjector 检索。绝不往生产库灌基准语料。
* --vector      给外部腿**接上向量道**（离线 bge-m3 + chroma，collection
                落 TemporaryDirectory）。给了却加载不上 → **非零退出**，
                绝不悄悄退回 fts-only 出数：换了引擎的数字配着旧描述，
                比明着失败危险得多（见卡一 SPEC 反向钉 7）。

输出：JSON 报告含 版本/时间/engine 元数据 + 各基准指标 + 逐案例面。
**engine 段的车道描述取自 QueryKernel.lane_profile().describe()**——
1.2.0 卡一切掉了原来那句手写的 `"fts-only trigram + RRF (no vector lane)"`：
它是散文，与引擎行为无绑定，接上向量道后仍会这么写，且永不暴露。
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
from mimir_v8.query import MAX_QUERY_LIMIT  # noqa: E402  ← 引擎硬顶单一真相源
from mimir_v8.schema import (CreateFact, MIMIR_VERSION, SCHEMA_VERSION,  # noqa: E402
                             register_agent)
from mimir_v8.store import CanonicalStore  # noqa: E402


class BenchmarkLaneUnavailable(RuntimeError):
    """请求了向量道但给不了。

    单独一个类型，是为了让 CLI 能**显式**接住它并非零退出，而不是让它混进
    某一层通用 except 里被吞掉。悄悄退回 fts-only 出数是本卡最坏的结局：
    报告写着「vector+fts+RRF」，数字其实只经过 fts。
    """


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


def _local_query_fn(store: CanonicalStore, key_to_fact: dict[str, str],
                    *, limit: int = 50, vector=None, embedder=None):
    """本地检索：question → 有序 session_key 列表。挡位由 vector 决定。"""
    from mimir_v8.projector import FTSProjector, ProjectorRunner
    from mimir_v8.query import QueryKernel, QueryRequest

    projector = FTSProjector(store.path.parent / "fts.db")
    runner = ProjectorRunner(store, projector)
    while True:  # outbox 排空（p28 同款姿势）
        drained = runner.run_once(limit=200)
        if not drained["processed"] or drained["failed"]:
            break
    kernel = QueryKernel(store, fts=projector, vector=vector,
                         embedder=embedder)
    fact_to_key = {fid: key for key, fid in key_to_fact.items()}
    profiles: list = []

    def fn(question: str) -> list[str]:
        # depth=deep：基准锚是 reference（L1 型），standard 装配层设计性
        # 不收 L1（v12.2.0 层门）——这里 deep 只是把基准与门语义对齐，
        # 金标腿照旧 standard（测量面不同，report.engine 分别标注）
        request = QueryRequest(
            text=question, principal_id="benchmark", limit=limit,
            use_vector=vector is not None, use_graph=False, depth="deep")
        if not profiles:
            # 首次调用即采样挡位：查询参数恒定，采样一次即可代表全腿。
            # 放在函数内是为了让「点了 --vector 却因断路器/缺依赖没点亮」
            # 这类事实在 engine 段里显形，而不是被用户事后从数字里猜。
            profiles.append(kernel.lane_profile(request))
        response = kernel.search(request)
        return [fact_to_key[r["fact_id"]] for r in response["results"]
                if r["fact_id"] in fact_to_key]

    fn.lane_profile = lambda: profiles[0] if profiles else None
    return fn


def _build_vector_lane(root: Path, corpus, key_to_fact: dict[str, str]):
    """离线 bge-m3 + chroma，投影基准语料。**给不了就抛，绝不退回 fts-only。**

    设备实测（2026-10-03）：加载 4.5s、1024 维、0.469 s/条、272 条 ≈ 128s。

    异常一律转成 :class:`BenchmarkLaneUnavailable`——CLI 捕获后非零退出。
    悄悄退回 fts-only 出数是最坏结局：engine 段会写「vector+fts+RRF」
    而数字其实只经过 fts，两者来自两套引擎却共用一份报告。
    """
    import os

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        import chromadb
        from chromadb.config import Settings
        from sentence_transformers import SentenceTransformer

        model_name = os.environ.get("MIMIR_V8_MODEL", "BAAI/bge-m3")
        client = chromadb.PersistentClient(
            path=str(root / "chroma"),
            settings=Settings(anonymized_telemetry=False))
        # staging 前缀：validate_vector_collection_name 认得的非 prod 命名，
        # 与生产 collection 物理隔离——基准语料绝不进生产向量库。
        collection = client.get_or_create_collection(
            name="mimir_v8_bench_shadow",
            metadata={"hnsw:space": "cosine", "projection": "staging"})
        model = SentenceTransformer(model_name, device="cpu",
                                    local_files_only=True)
    except Exception as exc:  # noqa: BLE001 — 一律归一成「车道不可用」
        raise BenchmarkLaneUnavailable(
            f"已请求 --vector 但向量道不可用：{exc!r}") from exc

    texts_by_key = {record.session_key: record.text for record in corpus.sessions}
    ids, texts, metadatas = [], [], []
    for key, fact_id in key_to_fact.items():
        text = texts_by_key.get(key)
        if not text:
            continue
        # 三张表同一次循环里推进：分两次拼时任一条被跳过就会错位，
        # 而 chroma 的 ids/embeddings/metadatas 长度不齐只在 upsert 时才炸。
        ids.append(fact_id)
        texts.append(text)
        metadatas.append({"session_key": key})
    if not ids:
        raise BenchmarkLaneUnavailable(
            "已请求 --vector 但语料投影为空——向量道将恒空，等于没接")
    try:
        embeds = _encode_batches(model, texts)
        collection.upsert(ids=ids, embeddings=embeds, documents=texts,
                          metadatas=metadatas)
    except Exception as exc:  # noqa: BLE001
        raise BenchmarkLaneUnavailable(
            f"已请求 --vector 但语料投影失败：{exc!r}") from exc
    return collection, _embedder_callable(model)


def _embedder_callable(model):
    """把 SentenceTransformer 包成生产同款 LockedEmbedder 语义（归一化 + tolist）。"""
    import threading

    lock = threading.RLock()

    def embed(text: str):
        with lock:
            out = model.encode(text, normalize_embeddings=True)
        return out.tolist() if hasattr(out, "tolist") else list(out)

    return embed


def _encode_batches(model, texts: list[str], batch: int = 16) -> list[list[float]]:
    """分批编码。一次性 encode 272 条会让内部批处理退化成单条，还容易顶到默认批上限。"""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch):
        out = model.encode(texts[start:start + batch], normalize_embeddings=True)
        if hasattr(out, "tolist"):
            out = out.tolist()
        vectors.extend(out)
    return vectors


def run_corpus(corpus_name: str, path: str, *,
                top_k=(1, 3, 5, 10), limit: int = 50, use_vector: bool = False):
    raw = load_json_path(path)
    corpus = (load_locomo(raw) if corpus_name == "locomo"
              else load_longmemeval(raw))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        store = CanonicalStore(tmp / "bench.db")
        key_to_fact = _ingest_sessions(store, corpus)
        vector = embedder = None
        if use_vector:
            vector, embedder = _build_vector_lane(tmp, corpus, key_to_fact)
        query_fn = _local_query_fn(store, key_to_fact, limit=limit,
                                   vector=vector, embedder=embedder)
        report = run_external_benchmark(corpus, query_fn, top_k_list=top_k)
        profile = getattr(query_fn, "lane_profile", lambda: None)()
    report["engine_lanes"] = None if profile is None else {
        "description": profile.describe(),
        "active": list(profile.active),
        "disabled": list(profile.disabled),
        "unavailable": list(profile.unavailable),
        "degraded": list(profile.degraded),
    }
    return report


def _parse_top_k(value: str) -> tuple[int, ...]:
    """--top-k 1,3,5,10,20,50 → (1,3,5,10,20,50)。

    宽 K 是刚需不是可选项：1.1.0 首跑 LoCoMo 时 median first_rank=9，
    默认 K=10 把「召回到了但排在第 10~14 位」全切掉了——同一引擎
    hit@10=0.46 而 hit@20/50 显著更高。不开这个开关就等于自己遮住上限。

    上界 ``MAX_QUERY_LIMIT`` 取 QueryRequest 的硬顶（100），越过它引擎
    逐条抛 ValueError——那会让 1977 个案例全变 degraded，报告指标段全 None，
    白跑一轮。与其在报告里读失败，不如在这里报错。
    """
    try:
        ks = tuple(sorted({int(part) for part in value.split(",") if part.strip()}))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--top-k 需为逗号分隔的正整数，收到 {value!r}")
    if not ks or any(k <= 0 for k in ks):
        raise argparse.ArgumentTypeError(f"--top-k 需全为正整数，收到 {value!r}")
    if ks[-1] > MAX_QUERY_LIMIT:
        raise argparse.ArgumentTypeError(
            f"--top-k 上界 {ks[-1]} 超过引擎硬顶 {MAX_QUERY_LIMIT}，"
            f"K 位拿不到结果（引擎会逐条抛错，报告全 None）")
    return ks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mímir 可复现跑分入口")
    parser.add_argument("--golden", action="store_true",
                        help="金标 24 条打活服务")
    parser.add_argument("--locomo", default=None, help="LOCOMO JSON 路径")
    parser.add_argument("--longmemeval", default=None,
                        help="LongMemEval JSON 路径")
    parser.add_argument("--out", default=None,
                        help="报告输出文件（缺省 stdout）")
    parser.add_argument("--top-k", type=_parse_top_k, default=(1, 3, 5, 10),
                        help="外部基准计分 K 列表，逗号分隔（默认 1,3,5,10）")
    parser.add_argument("--limit", type=int, default=50,
                        help=f"每次检索向引擎要多少条候选（默认 50，"
                             f"引擎硬顶 {MAX_QUERY_LIMIT}）")
    parser.add_argument("--vector", action="store_true",
                        help="给外部腿接上向量道（离线 bge-m3 + chroma）。"
                             "加载不上则非零退出，绝不悄悄退回 fts-only 出数")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= MAX_QUERY_LIMIT:
        parser.error(f"--limit 必须在 1..{MAX_QUERY_LIMIT}（引擎硬顶）"
                     f"，收到 {args.limit}")
    if args.limit < max(args.top_k):
        parser.error(f"--limit({args.limit}) 必须 >= max(--top-k)={max(args.top_k)}"
                     "，否则 K 位被候选截断，测的不是引擎能力")
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
        # 反向钉 7：--vector 说了要向量道，给不了就在出任何数字**之前**死。
        # 绝不让它落进通用 except 被吞——吞掉等于出一份「看起来测过」的报告。
        try:
            report["results"][name] = run_corpus(
                name, path, top_k=args.top_k, limit=args.limit,
                use_vector=args.vector)
        except BenchmarkLaneUnavailable as exc:
            parser.exit(2, f"跑分中止（未产出任何数字）：{exc}\n")
    engine = report["results"].get("locomo") or \
        report["results"].get("longmemeval")
    if engine:
        # engine 段的车道描述**只从 lane_profile 取**：1.2.0 卡一切掉了原来
        # 手写的 "fts-only trigram + RRF (no vector lane)" 散文——它与引擎
        # 行为无绑定，接上向量道后仍会照写，且永远不会有人发现。
        lanes = engine.get("engine_lanes") or {}
        report["engine"] = {
            "external": lanes.get("description") or "unknown (no lane profile)",
            "external_lanes": lanes.get("active", []),
            "golden": "live /v8/query full lanes",
            "external_top_k": list(args.top_k),
            "external_candidate_limit": args.limit,
        }

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
