# -*- coding: utf-8 -*-
"""LOCOMO / LongMemEval 外部基准 runner（1.1.0 Roadmap 差距#1）。

跑分先于一切架构投资——数字出来之前一切投资缺背书（09-11 用户 GO 里
的排序裁决）。两公开基准的 schema 各自解析成统一内部形态，计分共享：

内部形态：
    BenchmarkCorpus {benchmark, sessions: [SessionRecord], cases: [Case]}
    SessionRecord  {session_key, owner, text, occurred_at}
    Case           {question, golden_session_keys, group}

计分协议（会话级检索命中率，公开基准同款语义）：
* query(question) → top-k session_key 列表（query_fn 注入：生产真打
  走 scripts/run_benchmarks.py 的 HTTP 适配器；测试用桩手算裁判）
* hit@K：top-K 内出现任一 golden key；recall@K：|top-K ∩ golden|/|golden|
  （multi-session 案例 recall 才有信息量，单金标时两值退化相等）
* mrr：首个命中的 rank 倒数
* **abstention 案例（无 evidence/answer_session）显式剔除并计数**——
  空集不静默计分（与「空集必须显式报警」纪律同源）
* query_fn 抛错 → 该案例标 degraded 不计入分母（degraded≠miss，
  三态判语的基准面应用）

数据集文件不进仓（许可面）：runner 只吃路径/已加载对象。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Sequence

__all__ = ["SessionRecord", "Case", "BenchmarkCorpus",
           "load_locomo", "load_longmemeval", "run_external_benchmark",
           "load_json_path", "parse_locomo_date"]


@dataclass(frozen=True)
class SessionRecord:
    session_key: str
    owner: str
    text: str
    # 会话发生时间（ISO-8601 日期）。基准语料原本不读时间字段，时序面
    # 无数据可读——见 docs/plans/1.2.0-card2-temporal-lane.md §二。
    # None = 解析不出或源里没有；**不猜**。
    occurred_at: str | None = None


@dataclass(frozen=True)
class Case:
    question: str
    golden_session_keys: tuple[str, ...]
    group: str


@dataclass
class BenchmarkCorpus:
    benchmark: str
    sessions: list[SessionRecord] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    n_skipped_no_evidence: int = 0
    n_samples: int = 0
    # 解析不出发生时间的 session 数。恒等式：解析成功数 + 本数 == session
    # 总数（不得有第四种去向）。**解析率本身是结论的一部分**，必须留痕。
    n_sessions_without_date: int = 0


def load_json_path(path: str) -> Any:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


# ── LOCOMO ────────────────────────────────────────────────────────────────

_SESSION_RE = re.compile(r"^session_\d+$")

_MONTHS = {m.lower(): i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), start=1)}

# LoCoMo 的真实时间格式是自然语言：'1:56 pm on 8 May, 2023'。
# 允许缺时刻（'8 May, 2023'）与纯 ISO（'2023-05-08'）。
_LOCOMO_DATE_RE = re.compile(
    r"(?:\d{1,2}:\d{2}\s*(?:[ap]m)\s+on\s+)?"
    r"(\d{1,2})\s+([A-Za-z]{3,9}),?\s+(\d{4})")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def parse_locomo_date(raw: Any) -> str | None:
    """LoCoMo `session_N_date_time` → ISO-8601 日期串；解析不出返回 None。

    **解析不出来就是解析不出来**——本函数绝不猜。猜出来的日期会进排序
    且没人看得见它被猜了（判例：反向钉 7）。相对表达（"next tuesday"）、
    缺年份、月份名不认识，一律 None，由调用方计数留痕。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    iso = _ISO_DATE_RE.match(text)
    if iso:
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3])).isoformat()
        except ValueError:  # 2023-02-30 这类：判非法，不修
            return None
    m = _LOCOMO_DATE_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m[2].lower())
    if month is None:  # "8 Foo, 2023"：月份不认识 = 不认识
        return None
    try:
        return date(int(m[3]), month, int(m[1])).isoformat()
    except ValueError:  # 2 月 30 日：判非法，不修
        return None


def load_locomo(raw: Sequence[dict]) -> BenchmarkCorpus:
    """LOCOMO（快照或包裹形态）→ 统一 corpus。

    每条样本 conversation 里的 session_N 列表 = 一个 SessionRecord；
    qa.evidence 的 dia_id（"D{n}:m"）映射到 session_{n}——会话级检索
    判定（证据精确到 turn 的粒度 v1 不展开，展开属 deep 面）。
    evidence 为空的 qa（abstention，category 5）剔除计数。

    `session_N_date_time` 是会话发生时间，解析进 `occurred_at`；
    **解析失败计数留痕，session 照常入库**（丢整批会让检索面静默变窄）。
    """
    if isinstance(raw, dict):  # 兼容 {"data": [...]} 包裹形态
        raw = raw.get("data") or []
    corpus = BenchmarkCorpus(benchmark="locomo", n_samples=len(raw))
    for sample in raw:
        if not isinstance(sample, dict) or "conversation" not in sample \
                or "qa" not in sample:
            raise ValueError(f"locomo 样本缺 conversation/qa 段: "
                             f"{str(sample)[:60]!r}")
        sample_id = str(sample.get("sample_id", len(corpus.cases)))
        conversation = sample["conversation"]
        keys_by_dia: dict[str, str] = {}
        for field_name in sorted(conversation):
            if not _SESSION_RE.match(field_name):
                continue
            turns = conversation[field_name] or []
            lines = []
            for turn in turns:
                speaker = str(turn.get("speaker", ""))
                text = str(turn.get("text", ""))
                lines.append(f"{speaker}: {text}")
                dia = str(turn.get("dia_id", ""))
                if dia:
                    # "D1:2" → session_1
                    m = re.match(r"D(\d+):", dia)
                    if m:
                        keys_by_dia[dia] = (
                            f"locomo:{sample_id}:session_{m.group(1)}")
            occurred_at = parse_locomo_date(
                conversation.get(f"{field_name}_date_time"))
            if occurred_at is None:
                corpus.n_sessions_without_date += 1
            corpus.sessions.append(SessionRecord(
                session_key=f"locomo:{sample_id}:{field_name}",
                owner=str(conversation.get("speaker_a", "")),
                text="\n".join(lines),
                occurred_at=occurred_at))
        for qa in sample["qa"] or []:
            evidence = qa.get("evidence") or []
            if not evidence:
                corpus.n_skipped_no_evidence += 1
                continue
            golden = sorted({keys_by_dia[e] for e in evidence
                             if e in keys_by_dia})
            if not golden:
                corpus.n_skipped_no_evidence += 1
                continue
            corpus.cases.append(Case(
                question=str(qa.get("question", "")),
                golden_session_keys=tuple(golden),
                group=str(qa.get("category", "all"))))
    return corpus


# ── LongMemEval ───────────────────────────────────────────────────────────

def load_longmemeval(raw: Sequence[dict]) -> BenchmarkCorpus:
    """LongMemEval → 统一 corpus。

    haystack_sessions 逐条建 SessionRecord（answer_session_ids 给的金标
    必须能对上 haystack 里的行——对不上即 schema 错误，大声拒绝而非静默
    丢案例）。question_type 作分组面。
    """
    if isinstance(raw, dict):
        raw = raw.get("data") or raw.get("items") or []
    corpus = BenchmarkCorpus(benchmark="longmemeval", n_samples=len(raw))
    for item in raw:
        if not isinstance(item, dict) or "haystack_sessions" not in item \
                or "question" not in item:
            raise ValueError(f"longmemeval 条目缺必需段: {str(item)[:60]!r}")
        qid = str(item.get("question_id", len(corpus.cases)))
        haystack_ids = list(item.get("haystack_session_ids")
                            or range(len(item["haystack_sessions"])))
        known_keys: set[str] = set()
        for sid, turns in zip(haystack_ids, item["haystack_sessions"]):
            lines = [f"{t.get('role','')}: {t.get('content','')}"
                     for t in (turns or [])]
            key = f"lme:{qid}:{sid}"
            known_keys.add(key)
            corpus.sessions.append(SessionRecord(
                session_key=key,
                owner=str(item.get("answer_family", "user")),
                text="\n".join(lines)))
        answer_ids = item.get("answer_session_ids") or []
        if not answer_ids:
            corpus.n_skipped_no_evidence += 1
            continue
        golden = [f"lme:{qid}:{aid}" for aid in answer_ids]
        missing = [g for g in golden if g not in known_keys]
        if missing:
            raise ValueError(
                f"longmemeval {qid}: answer_session_ids 对不上 "
                f"haystack（{missing[:3]}）——schema 损坏，拒绝半包")
        corpus.cases.append(Case(
            question=str(item["question"]),
            golden_session_keys=tuple(sorted(golden)),
            group=str(item.get("question_type", "all"))))
    return corpus


# ── 计分 ──────────────────────────────────────────────────────────────────

def run_external_benchmark(corpus: BenchmarkCorpus,
                           query_fn: Callable[[str], Sequence[str]],
                           *, top_k_list: Iterable[int] = (1, 3, 5, 10),
                           limit: int = 100) -> dict[str, Any]:
    """对 corpus 逐案例跑检索并出报告（纯函数，可测可复算）。

    query_fn(question) -> 有序 session_key 列表（长度不设限，K 截断在
    计分侧做——limit 只是提示注入实现向服务端要多少条）。
    """
    top_k_list = tuple(top_k_list)
    per_case: list[dict[str, Any]] = []
    n_errors = 0
    for case in corpus.cases:
        try:
            retrieved = list(query_fn(case.question))
        except Exception as exc:  # noqa: BLE001 — 通道故障三态：degraded
            n_errors += 1
            per_case.append({"question": case.question, "group": case.group,
                             "status": "degraded", "error": str(exc)[:120]})
            continue
        golden = set(case.golden_session_keys)
        ranks = [rank for rank, key in enumerate(retrieved, start=1)
                 if key in golden]
        entry: dict[str, Any] = {"question": case.question,
                                 "group": case.group, "status": "scored",
                                 "first_rank": ranks[0] if ranks else None}
        for k in top_k_list:
            top = set(retrieved[:k])
            entry[f"hit@{k}"] = 1.0 if top & golden else 0.0
            entry[f"recall@{k}"] = len(top & golden) / len(golden)
        entry["rr"] = 1.0 / ranks[0] if ranks else 0.0
        per_case.append(entry)

    scored = [c for c in per_case if c["status"] == "scored"]
    metrics: dict[str, float] = {}
    if scored:
        for k in top_k_list:
            metrics[f"hit_rate@{k}"] = (
                sum(c[f"hit@{k}"] for c in scored) / len(scored))
            metrics[f"recall@{k}"] = (
                sum(c[f"recall@{k}"] for c in scored) / len(scored))
        metrics["mrr"] = sum(c["rr"] for c in scored) / len(scored)
    else:
        # 全错/全剔除——指标段诚实缺席，不得输出零冒充「测过」
        metrics = {f"{n}@{k}": None
                   for n in ("hit_rate", "recall") for k in top_k_list}
        metrics["mrr"] = None

    by_group: dict[str, dict[str, float]] = {}
    for group in sorted({c["group"] for c in per_case}):
        rows = [c for c in scored if c["group"] == group]
        if not rows:
            by_group[group] = {"n": 0}
            continue
        by_group[group] = {
            **{f"hit_rate@{k}": sum(c[f"hit@{k}"] for c in rows) / len(rows)
               for k in top_k_list},
            "mrr": sum(c["rr"] for c in rows) / len(rows),
            "n": len(rows),
        }
    return {
        "benchmark": corpus.benchmark,
        "provenance": corpus.benchmark,  # 外部数据=独立 provenance 面
        "summary": {
            "n_cases": len(corpus.cases),
            "n_scored": len(scored),
            "n_query_errors": n_errors,
            "n_skipped_no_evidence": corpus.n_skipped_no_evidence,
            "n_sessions": len(corpus.sessions),
            "top_k_list": list(top_k_list),
            "metrics": metrics,
        },
        "by_group": by_group,
        "cases": per_case,
    }
