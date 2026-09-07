"""ACL-first hybrid query kernel for Mímir v8."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .store import CanonicalStore
from .relevance import RelevanceGate
from .schema import DECAY_HALF_LIFE
from .dedup import jaccard_similarity


class CircuitBreaker:
    """P46 (v14.1.0) 检索通道三态断路器（借自 aduMEI v20.2 自动挡位引擎）。

    CLOSED（健康）→ 连续 FAILURE_THRESHOLD 次失败 → OPEN（跳过通道调用，
    不再撞死马）→ 冷却 COOLDOWN_SECONDS 到期 → HALF_OPEN（放一次真实
    探测）→ 成功回 CLOSED / 失败回 OPEN 重新计冷却。单通道故障被隔离在
    通道内：检索面永不因某后端 500，其余通道与锚通道照常服务。

    生产判例（P45 同族）：投影器线程一次 sqlite 锁异常即静默死亡 4 天；
    同理，vector(chroma)/fts 任一后端抖动不该有能力塌掉整个检索面。
    """

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.state = "closed"
        self._failures = 0
        self._opened_at: float | None = None

    def allow_call(self, *, now: float | None = None) -> bool:
        """CLOSED/HALF_OPEN 放行调用；OPEN 时冷却到期则转 HALF_OPEN 放一次探测。"""
        if self.state == "closed":
            return True
        if self.state == "half_open":
            return True  # 单次探测已在飞；后续调用仍走 OPEN 语义由 on_failure/on_success 定
        # state == "open"
        current = time.monotonic() if now is None else now
        if self._opened_at is not None and (current - self._opened_at) >= self.cooldown_seconds:
            self.state = "half_open"
            return True
        return False

    def on_success(self) -> None:
        self.state = "closed"
        self._failures = 0
        self._opened_at = None

    def on_failure(self, *, now: float | None = None) -> None:
        self._failures += 1
        if self.state == "half_open":
            # 探测失败：回 OPEN 重新计冷却（不放行潮水流量）。
            self.state = "open"
            self._opened_at = time.monotonic() if now is None else now
            return
        if self._failures >= self.failure_threshold:
            self.state = "open"
            self._opened_at = time.monotonic() if now is None else now


@dataclass(frozen=True)
class QueryRequest:
    text: str
    principal_id: str
    limit: int = 10
    candidate_limit: int = 50
    roles: tuple[str, ...] = ()
    is_admin: bool = False
    owner_principal: str | None = None
    domain: str | None = None
    fact_type: str | None = None
    use_vector: bool = True
    use_fts: bool = True
    use_graph: bool = True
    include_provisional: bool = False
    #: v12.2.0 Anchor Channel: iron rules and core user preferences are
    #: injected into the candidate pool directly from the canonical store,
    #: immune to semantic-similarity veto by the vector/fts/graph channels.
    use_anchor: bool = True
    #: v12.2.0 Layered assembly depth. "standard" assembles only the L3+L2
    #: default surface (persona/iron rules, preferences, crystallized
    #: patterns); "deep" additionally drills down into L1 atom facts for
    #: evidence-grade tracebacks. L0 conversations are never assembled by
    #: retrieval — they are reached only via explicit trace endpoints.
    depth: str = "standard"


class QueryKernel:
    #: RRF channel weights — vector (bge-m3) carries the primary semantic
    #: signal, fts is strong for exact terminology, graph is weakest until
    #: entity–entity relations exist (currently tag-only). The anchor
    #: channel is deliberately weighted below vector: it guarantees entry
    #: into the candidate pool, not top placement.
    CHANNEL_WEIGHTS = {"vector": 1.0, "fts": 0.85, "graph": 0.6, "anchor": 0.5}
    #: Fact types that ride the anchor channel: iron rules (L0_never) and
    #: core user preferences (L1_preference) are the system's safety floor
    #: and must never be lost to a similarity threshold veto.
    #: v14.0 compiled skills ride the anchor channel too — they are
    #: L3 by construction (competence-gated, human-approved) and must
    #: not be lost to a similarity veto, same as the safety floor.
    ANCHOR_FACT_TYPES = ("iron_rule", "user_pref", "skill")
    #: Upper bound on anchor injection per query — the channel guarantees
    #: presence, not unbounded crowding of the candidate pool.
    ANCHOR_BUDGET = 20
    #: v12.2.0 L0~L3 layered memory mapping (zero-migration: derived from
    #: the existing facts.fact_type enum). L3 = persona / iron rules;
    #: L2 = crystallized scenarios / wiki blocks; L1 = atom facts, which
    #: standard-depth retrieval deliberately skips to cut token cost —
    #: only depth="deep" tracebacks drill into them. L0 (raw conversations)
    #: lives in conversation_messages and is never assembled by search().
    #: v14.0 skills (compiled from competent traces) join the L3
    #: surface — always-on assembly, same presence contract as the
    #: persona anchors.
    LAYER3_FACT_TYPES = ("iron_rule", "user_pref", "skill")
    LAYER2_FACT_TYPES = ("pattern",)
    LAYER1_FACT_TYPES = ("event", "project_config", "ephemeral", "learning", "reference")
    #: The layer-2 injection budget mirrors the anchor budget idea: presence
    #: for crystallized patterns, bounded crowding.
    LAYER2_BUDGET = 10
    DEPTHS = ("standard", "deep")
    #: P46 韧性挡位：断路器跳闸阈值与冷却窗。连续 FAILURE_THRESHOLD 次
    #: 通道异常 → OPEN 跳过该通道调用（省资源+止损），COOLDOWN_SECONDS
    #: 后 HALF_OPEN 放一次真实探测，成功回 CLOSED。锚通道不走断路器
    #: （锚通道直查 canonical，不依赖相似度后端——全后端炸时的安全底线）。
    FAILURE_THRESHOLD = 3
    COOLDOWN_SECONDS = 60.0

    def __init__(self, store: CanonicalStore, *, vector=None, fts=None, graph=None,
                 embedder=None, rrf_k: int = 60):
        self.store = store
        self.vector = vector
        self.fts = fts
        self.graph = graph
        self.embedder = embedder
        self.rrf_k = rrf_k
        # P46: 每个相似度通道一个独立断路器；self.breakers 是公开观测面
        # （/ready 健康巡检与 ops health 可直读跳闸状态）。
        self.breakers = {
            "vector": CircuitBreaker(failure_threshold=self.FAILURE_THRESHOLD,
                                     cooldown_seconds=self.COOLDOWN_SECONDS),
            "fts": CircuitBreaker(failure_threshold=self.FAILURE_THRESHOLD,
                                  cooldown_seconds=self.COOLDOWN_SECONDS),
            "graph": CircuitBreaker(failure_threshold=self.FAILURE_THRESHOLD,
                                    cooldown_seconds=self.COOLDOWN_SECONDS),
        }

    # ── P46 韧性挡位：通道调用统一过断路器 ─────────────────────
    def _query_vector(self, query: str, candidate_limit: int) -> tuple[list[str], list[float]]:
        """vector 通道单次调用（embedder 编码 + chroma 查询都在断路器保护链内）。"""
        embedding = self.embedder(query)
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        raw = self.vector.query(
            query_embeddings=[embedding], n_results=candidate_limit,
            include=["distances"],
        )
        ids = (raw.get("ids") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        return list(ids), list(distances)

    def _run_channel(self, name: str, call) -> tuple[list[str], str]:
        """执行一个相似度通道调用，返回 (ids, channel_state)。

        - 断路器 OPEN 且未到冷却：跳过调用，直接 (ids=[], state="open")
        - 调用异常：记失败（断路器计数/可能跳闸），(ids=[], state="degraded")
        - 调用成功：断路器 on_success 复位，state="closed"
        锚通道不走此路径（直查 canonical，不依赖相似度后端）。
        """
        breaker = self.breakers.get(name)
        if breaker is not None and not breaker.allow_call():
            return [], "open"
        try:
            ids = call()
        except Exception:
            if breaker is not None:
                breaker.on_failure()
            return [], "degraded"
        if breaker is not None:
            breaker.on_success()
        return list(ids or []), "closed"

    def _channel_states(self, states: dict[str, str], *, anchor: str, layer: bool) -> dict[str, str]:
        """响应 channels 三态视图（closed/degraded/open）+ 锚/装配层态。"""
        channels = dict(states)
        channels["anchor"] = anchor
        channels["layer"] = "closed" if layer else "off"
        return channels

    def search(self, request: QueryRequest) -> dict:
        query = request.text.strip()
        if not query:
            return {
                "results": [], "total": 0, "filtered": {"acl": 0, "status": 0},
                "gate": {"skipped": True, "reason": "empty query"},
            }
        should_search = True
        reason = ""
        try:
            gate = RelevanceGate()
            should_search, reason = gate.should_search(query)
        except Exception:
            should_search = True
            reason = "gate-error-fallback"
        if not should_search:
            return {
                "results": [], "total": 0, "filtered": {"acl": 0, "status": 0},
                "gate": {"skipped": True, "reason": reason},
            }
        if not 1 <= request.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if request.depth not in self.DEPTHS:
            raise ValueError(f"depth must be one of {self.DEPTHS}")
        candidate_limit = max(request.limit, min(request.candidate_limit, 500))
        ranked: dict[str, dict] = {}
        channel_states: dict[str, str] = {}

        if request.use_vector and self.vector is not None and self.embedder is not None:
            pair = self._run_channel("vector", lambda: self._query_vector(query, candidate_limit))
            channel_states["vector"] = pair[1]
            if pair[0]:
                ids, distances = pair[0]
                self._add_ranked(ranked, "vector", ids, distances)
        else:
            channel_states["vector"] = "off"

        if request.use_fts and self.fts is not None:
            ids, state = self._run_channel("fts", lambda: self.fts.search_ids(query, limit=candidate_limit))
            channel_states["fts"] = state
            if ids:
                self._add_ranked(ranked, "fts", ids)
        else:
            channel_states["fts"] = "off"

        if request.use_graph and self.graph is not None:
            seeds = [fact_id for fact_id, _ in sorted(
                ranked.items(), key=lambda item: (-item[1]["rrf"], item[0])
            )[: min(10, candidate_limit)]]
            graph_ids, state = self._run_channel(
                "graph", lambda: self._graph_neighbors(seeds, candidate_limit)
            )
            channel_states["graph"] = state
            if graph_ids:
                self._add_ranked(ranked, "graph", graph_ids)
        else:
            channel_states["graph"] = "off"

        anchor_injected = 0
        if request.use_anchor:
            anchor_ids = self._anchor_fact_ids(request, ranked)
            anchor_injected = len(anchor_ids)
            self._add_ranked(ranked, "anchor", anchor_ids)

        # v12.2.0 progressive layer sweep. Standard depth assembles the
        # L3+L2 surface (persona anchors + crystallized patterns); deep
        # additionally sweeps L1 atom facts for evidence-grade tracebacks.
        # The sweep guarantees presence within a budget, mirroring the
        # anchor channel contract — ACL still arbitrates at hydration.
        sweep_types, sweep_budget = self._layer_sweep_spec(request)
        sweep_injected = 0
        if sweep_types:
            sweep_ids = self._layer_sweep_ids(request, ranked, sweep_types, sweep_budget)
            sweep_injected = len(sweep_ids)
            self._add_ranked(ranked, "layer", sweep_ids)

        results = []
        filtered = {"acl": 0, "status": 0, "missing": 0, "layer": 0}
        roles = set(request.roles)
        for fact_id, score in ranked.items():
            try:
                fact = self.store.get_fact(fact_id)
            except Exception:
                filtered["missing"] += 1
                continue
            if fact["status"] not in ("active", "provisional") if request.include_provisional else fact["status"] != "active":
                filtered["status"] += 1
                continue
            if request.owner_principal and fact["owner_principal"] != request.owner_principal:
                continue
            if request.domain and fact["domain"] != request.domain:
                continue
            if request.fact_type and fact["fact_type"] != request.fact_type:
                continue
            # v12.2.0 L1 gate: standard-depth retrieval keeps the assembly
            # surface at L3+L2. L1 atom facts that slipped into the pool via
            # a similarity channel are dropped here (and counted) unless the
            # caller drills deep or names an explicit fact_type.
            if (
                request.depth == "standard"
                and not request.fact_type
                and fact["fact_type"] in self.LAYER1_FACT_TYPES
            ):
                filtered["layer"] += 1
                continue
            if not self.store.can_read(
                fact_id, request.principal_id, is_admin=request.is_admin, roles=roles
            ):
                filtered["acl"] += 1
                continue
            confidence = fact["confidence_score"] if fact["confidence_score"] is not None else 0.5
            decay = self._decay_factor(fact.get("decay_tier"), fact.get("valid_to"), fact.get("updated_at"), fact.get("valid_from"))
            freshness = self._freshness(fact["updated_at"])
            not_yet_effective = self._not_yet_effective(fact.get("valid_from"))
            final = score["rrf"] * (0.70 + 0.20 * confidence + 0.10 * freshness) * (0.60 + 0.40 * decay)
            results.append({
                "fact_id": fact_id,
                "content": fact["content"],
                "summary": fact["summary"],
                "owner_principal": fact["owner_principal"],
                "domain": fact["domain"],
                "fact_type": fact["fact_type"],
                "version": fact["current_version"],
                "score": round(final, 8),
                "score_explanation": {
                    "rrf": round(score["rrf"], 8),
                    "channels": score["channels"],
                    "confidence": confidence,
                    "freshness": round(freshness, 4),
                    "decay_factor": round(decay, 4),
                    "not_yet_effective": not_yet_effective,
                    "acl_hydrated_from": "canonical",
                },
            })
        results.sort(key=lambda item: (item["score_explanation"]["not_yet_effective"], -item["score"], item["fact_id"]))

        fact_ids = [r["fact_id"] for r in results[: request.limit]]
        opinions_map: dict[str, list[dict]] = {}
        if fact_ids:
            try:
                from .opinion import OpinionService
                op_svc = OpinionService(self.store)
                for op in op_svc.get_opinions_for_facts(fact_ids):
                    fact_id = op["fact_id"]
                    if fact_id not in opinions_map:
                        opinions_map[fact_id] = []
                    opinions_map[fact_id].append({
                        "topic": op["topic"],
                        "stance": op["stance"],
                        "confidence": op["confidence"],
                    })
            except Exception:
                pass
        for r in results:
            r["opinions"] = opinions_map.get(r["fact_id"], [])

        # P46 诚实遥测：channels 三态（closed/degraded/open/off），顶层
        # degraded 在任一相似度通道异常或跳闸时为真——消费者可感知降级
        # 而非被 500 打脸；全健康时恒 False（防假阳性）。
        channels = self._channel_states(
            channel_states,
            anchor="closed" if request.use_anchor else "off",
            layer=bool(sweep_types),
        )
        degraded = any(s == "degraded" or s == "open" for s in channels.values())
        return {
            "query": query,
            "principal_id": request.principal_id,
            "results": results[: request.limit],
            "candidate_count": len(ranked),
            "filtered": filtered,
            "filters": {
                "owner_principal": request.owner_principal,
                "domain": request.domain,
                "fact_type": request.fact_type,
                "include_provisional": request.include_provisional,
                "depth": request.depth,
            },
            "channels": channels,
            "degraded": degraded,
            "anchor": {"injected": anchor_injected},
            "layers": {"injected": sweep_injected,
                       "l1_dropped": filtered["layer"],
                       "depth": request.depth},
        }

    def trace(self, request: QueryRequest, *, dedup_threshold: float = 0.8) -> dict:
        """v12 recall funnel trace: candidate pool → gate → Jaccard dedup →
        Chronos decay → top-K. Reports per-stage timing, hits and decay factors."""
        import time as _time
        query = request.text.strip()
        stages = []

        def stage(name: str, *, total: int, keep: int, extra: dict | None = None) -> None:
            stages.append({
                "stage": name,
                "total": total,
                "hit": keep,
                "dropped": total - keep,
                "elapsed_ms": round((_time.monotonic() - _t0) * 1000, 2),
                **(extra or {}),
            })

        _t0 = _time.monotonic()
        if not query:
            return {"query": query, "skipped": True, "reason": "empty query", "stages": []}

        # ── Stage 1: RelevanceGate ───────────────────────────────
        gate_pass = True
        gate_reason = ""
        try:
            _gate = RelevanceGate()
            gate_pass, gate_reason = _gate.should_search(query)
        except Exception:
            pass
        stage("RelevanceGate", total=1, keep=1 if gate_pass else 0,
              extra={"reason": gate_reason})
        if not gate_pass:
            return {"query": query, "skipped": True, "reason": gate_reason, "stages": stages}

        # ── Stage 2: candidate pool (vector+fts+graph) ───────────
        # P46: 与 search() 同语义——三通道过断路器，单通道异常只降级该通道。
        candidate_limit = max(request.limit, min(request.candidate_limit, 500))
        ranked: dict[str, dict] = {}
        channel_states: dict[str, str] = {}
        if request.use_vector and self.vector is not None and self.embedder is not None:
            pair = self._run_channel("vector", lambda: self._query_vector(query, candidate_limit))
            channel_states["vector"] = pair[1]
            if pair[0]:
                ids, distances = pair[0]
                self._add_ranked(ranked, "vector", ids, distances)
        else:
            channel_states["vector"] = "off"
        if request.use_fts and self.fts is not None:
            ids, state = self._run_channel("fts", lambda: self.fts.search_ids(query, limit=candidate_limit))
            channel_states["fts"] = state
            if ids:
                self._add_ranked(ranked, "fts", ids)
        else:
            channel_states["fts"] = "off"
        if request.use_graph and self.graph is not None:
            seeds = [fact_id for fact_id, _ in sorted(
                ranked.items(), key=lambda item: (-item[1]["rrf"], item[0])
            )[: min(10, candidate_limit)]]
            graph_ids, state = self._run_channel(
                "graph", lambda: self._graph_neighbors(seeds, candidate_limit)
            )
            channel_states["graph"] = state
            if graph_ids:
                self._add_ranked(ranked, "graph", graph_ids)
        else:
            channel_states["graph"] = "off"
        anchor_injected = 0
        if request.use_anchor:
            anchor_ids = self._anchor_fact_ids(request, ranked)
            anchor_injected = len(anchor_ids)
            self._add_ranked(ranked, "anchor", anchor_ids)
        # v12.2.0 progressive layer sweep — same semantics as search().
        sweep_types, sweep_budget = self._layer_sweep_spec(request)
        sweep_injected = 0
        if sweep_types:
            sweep_ids = self._layer_sweep_ids(request, ranked, sweep_types, sweep_budget)
            sweep_injected = len(sweep_ids)
            self._add_ranked(ranked, "layer", sweep_ids)
        stage("CandidatePool", total=len(ranked), keep=len(ranked),
              extra={"channels": {name: s for name, s in channel_states.items()
                                  if s != "off"}})
        stage("AnchorChannel", total=anchor_injected, keep=anchor_injected,
              extra={"injected": anchor_injected,
                     "fact_types": list(self.ANCHOR_FACT_TYPES),
                     "enabled": request.use_anchor})
        stage("LayerSweep", total=sweep_injected, keep=sweep_injected,
              extra={"injected": sweep_injected, "depth": request.depth,
                     "fact_types": list(sweep_types) if sweep_types else []})

        # Hydrate facts once for the whole funnel.
        facts: dict[str, dict] = {}
        roles = set(request.roles)
        for fact_id in ranked:
            try:
                fact = self.store.get_fact(fact_id)
            except Exception:
                continue
            if fact["status"] not in ("active", "provisional") if request.include_provisional else fact["status"] != "active":
                continue
            if request.owner_principal and fact["owner_principal"] != request.owner_principal:
                continue
            if request.fact_type and fact["fact_type"] != request.fact_type:
                continue
            # v12.2.0 L1 gate — mirrors search() so trace and search agree.
            if (
                request.depth == "standard"
                and not request.fact_type
                and fact["fact_type"] in self.LAYER1_FACT_TYPES
            ):
                continue
            if not self.store.can_read(
                fact_id, request.principal_id, is_admin=request.is_admin, roles=roles
            ):
                continue
            facts[fact_id] = fact
        pool = list(facts.items())

        # ── Stage 3: Jaccard dedup ───────────────────────────────
        deduped: list[tuple[str, dict]] = []
        for fid, fact in pool:
            dup = False
            for kfid, kfact in deduped:
                if jaccard_similarity(fact["content"], kfact["content"]) >= dedup_threshold:
                    dup = True
                    break
            if not dup:
                deduped.append((fid, fact))
        stage("JaccardDedup", total=len(pool), keep=len(deduped),
              extra={"threshold": dedup_threshold})

        # ── Stage 4: Chronos decay ───────────────────────────────
        decayed_hits = 0
        expired = 0
        total_decay = 0.0
        for fid, fact in deduped:
            factor = self._decay_factor(
                fact.get("decay_tier"), fact.get("valid_to"),
                fact.get("updated_at"), fact.get("valid_from"),
            )
            total_decay += factor
            if factor <= 0.5 and fact.get("valid_to"):
                expired += 1
        avg_factor = round(total_decay / len(deduped), 4) if deduped else 0.0
        decayed_hits = len(deduped)
        stage("ChronosDecay", total=len(deduped), keep=decayed_hits,
              extra={"avg_decay_factor": avg_factor, "expired": expired})

        # ── Stage 5: top-K ranking ───────────────────────────────
        results = []
        for fid, fact in deduped:
            score = ranked[fid]["rrf"]
            confidence = fact["confidence_score"] if fact["confidence_score"] is not None else 0.5
            decay = self._decay_factor(
                fact.get("decay_tier"), fact.get("valid_to"),
                fact.get("updated_at"), fact.get("valid_from"),
            )
            freshness = self._freshness(fact["updated_at"])
            not_yet_effective = self._not_yet_effective(fact.get("valid_from"))
            final = score * (0.70 + 0.20 * confidence + 0.10 * freshness) * (0.60 + 0.40 * decay)
            results.append({
                "fact_id": fid,
                "content": fact["content"],
                "summary": fact["summary"],
                "score": round(final, 8),
                "decay_factor": round(decay, 4),
                "not_yet_effective": not_yet_effective,
            })
        results.sort(key=lambda item: (item["not_yet_effective"], -item["score"], item["fact_id"]))
        top = results[: request.limit]
        stage("TopK", total=len(results), keep=len(top),
              extra={"limit": request.limit, "k": len(top)})

        # P46 诚实遥测：trace 与 search 两口径不分叉——channels 三态+顶层 degraded。
        channels = self._channel_states(
            channel_states,
            anchor="closed" if request.use_anchor else "off",
            layer=bool(sweep_types),
        )
        return {
            "query": query,
            "skipped": False,
            "stages": stages,
            "results": top,
            "total_candidates": len(pool),
            "total_results": len(results),
            "channels": channels,
            "degraded": any(s == "degraded" or s == "open" for s in channels.values()),
        }

    def _layer_sweep_spec(self, request: QueryRequest) -> tuple[tuple[str, ...], int]:
        """Which non-anchor layers the progressive sweep covers.

        An explicit fact_type expresses precise traceback intent and
        overrides the depth default — whatever type is named gets swept
        regardless of its layer (with the anchor types already handled by
        the anchor channel, a named non-anchor type is the only way an
        L1/L2 type can ride the sweep).
        """
        if request.fact_type:
            if request.fact_type in self.ANCHOR_FACT_TYPES:
                return (), 0
            return (request.fact_type,), self.LAYER2_BUDGET
        if request.depth == "deep":
            return self.LAYER2_FACT_TYPES + self.LAYER1_FACT_TYPES, self.LAYER2_BUDGET
        return self.LAYER2_FACT_TYPES, self.LAYER2_BUDGET

    def _layer_sweep_ids(self, request: QueryRequest, ranked: dict,
                         types: tuple[str, ...], budget: int) -> list[str]:
        """v12.2.0 progressive layer sweep: pull the depth's surface from
        the canonical store so it enters the pool regardless of channel
        similarity — same presence-within-budget contract as the anchor
        channel; ACL still arbitrates at canonical hydration."""
        where = ["status='active'", "fact_type IN ({})".format(
            ",".join("?" for _ in types))]
        params: list = list(types)
        if request.owner_principal:
            where.append("owner_principal=?")
            params.append(request.owner_principal)
        if request.domain:
            where.append("domain=?")
            params.append(request.domain)
        params.append(budget)
        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute(
                "SELECT fact_id FROM facts WHERE {} "
                "ORDER BY updated_at DESC, fact_id LIMIT ?".format(
                    " AND ".join(where)),
                params,
            ).fetchall()
        fresh = [row["fact_id"] for row in rows]
        return [fact_id for fact_id in fresh if fact_id not in ranked]

    def _anchor_fact_ids(self, request: QueryRequest, ranked: dict) -> list[str]:
        """v12.2.0 Anchor Channel: pull active iron rules and core user
        preferences straight from the canonical store so they enter the
        preferences straight from the canonical store so they enter the
        candidate pool regardless of vector/fts similarity ranking.

        Explicit caller filters (owner/domain/fact_type) are honoured here —
        the anchor channel changes who enters the pool, not who can be read;
        ACL arbitration still happens during canonical hydration, so
        owner_only iron rules of other agents cannot leak through it.
        """
        types = list(self.ANCHOR_FACT_TYPES)
        if request.fact_type:
            # Explicit fact_type filter expresses searcher intent; an anchor
            # type passes through, anything else disqualifies every anchor.
            if request.fact_type in self.ANCHOR_FACT_TYPES:
                types = [request.fact_type]
            else:
                return []
        where = ["status='active'", "fact_type IN ({})".format(
            ",".join("?" for _ in types))]
        params: list = list(types)
        if request.owner_principal:
            where.append("owner_principal=?")
            params.append(request.owner_principal)
        if request.domain:
            where.append("domain=?")
            params.append(request.domain)
        params.append(self.ANCHOR_BUDGET)
        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute(
                "SELECT fact_id FROM facts WHERE {} "
                "ORDER BY updated_at DESC, fact_id LIMIT ?".format(
                    " AND ".join(where)),
                params,
            ).fetchall()
        # Freshness ordering: most recently updated anchors win the budget.
        fresh = [row["fact_id"] for row in rows]
        # Facts already surfaced by a similarity channel keep their richer
        # provenance — only inject what the channels missed (the veto case).
        return [fact_id for fact_id in fresh if fact_id not in ranked]

    def _add_ranked(self, ranked: dict, channel: str, ids, distances=None) -> None:
        weight = self.CHANNEL_WEIGHTS.get(channel, 1.0)
        for rank, fact_id in enumerate(ids, 1):
            state = ranked.setdefault(fact_id, {"rrf": 0.0, "channels": {}})
            contribution = weight / (self.rrf_k + rank)
            state["rrf"] += contribution
            detail = {"rank": rank, "rrf_contribution": round(contribution, 8)}
            if distances is not None and rank <= len(distances):
                detail["distance"] = round(float(distances[rank - 1]), 8)
            state["channels"][channel] = detail

    def _graph_neighbors(self, seeds: list[str], limit: int) -> list[str]:
        if not seeds:
            return []
        placeholders = ",".join("?" for _ in seeds)
        with contextlib.closing(self.graph.connect()) as connection:
            rows = connection.execute(
                f"""SELECT source_fact_id, target_id FROM graph_edges
                WHERE status='active' AND target_type='fact'
                AND (source_fact_id IN ({placeholders}) OR target_id IN ({placeholders}))
                ORDER BY relation_id LIMIT ?""",
                (*seeds, *seeds, limit * 2),
            ).fetchall()
        result = []
        seen = set(seeds)
        for row in rows:
            for fact_id in (row["source_fact_id"], row["target_id"]):
                if fact_id not in seen:
                    seen.add(fact_id)
                    result.append(fact_id)
                    if len(result) >= limit:
                        return result
        return result

    @staticmethod
    def _freshness(updated_at: str) -> float:
        if not updated_at:
            return 0.0
        try:
            from datetime import datetime, timezone
            updated = datetime.fromisoformat(updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - updated).days
            return 1.0 / (1.0 + age_days / 365.0)
        except Exception:
            return 0.0

    @staticmethod
    def _not_yet_effective(valid_from: str | None) -> bool:
        """Chronos: a fact whose valid_from is in the future is not yet in force."""
        if not valid_from:
            return False
        try:
            from datetime import datetime, timezone
            vf = datetime.fromisoformat(valid_from)
            if vf.tzinfo is None:
                vf = vf.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < vf
        except Exception:
            return False

    @staticmethod
    def _decay_factor(decay_tier: str | None, valid_to: str | None, updated_at: str, valid_from: str | None = None) -> float:
        """Calculate decay factor based on tier and elapsed time.
        Returns 1.0 (no decay) for L0_never, scales down to 0.0 for fully decayed.

        v12 Chronos double-timeline: a fact past its valid_to is deprioritized by 50%
        on top of the Ebbinghaus decay. Expired facts are deweighted, not deleted.
        """
        from datetime import datetime, timezone
        factor = 1.0
        # Chronos: valid_to expiry deweights 50%
        if valid_to:
            try:
                vt = datetime.fromisoformat(valid_to)
                if vt.tzinfo is None:
                    vt = vt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > vt:
                    factor *= 0.5
            except Exception:
                pass
        if decay_tier == "L0_never":
            return factor
        half_life = DECAY_HALF_LIFE.get(decay_tier, 30)
        if half_life is None or half_life <= 0:
            return factor
        try:
            updated = datetime.fromisoformat(updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            elapsed_days = (datetime.now(timezone.utc) - updated).days
        except Exception:
            elapsed_days = 0
        decay = 2.0 ** (-elapsed_days / half_life)
        return max(0.01, factor * decay)
