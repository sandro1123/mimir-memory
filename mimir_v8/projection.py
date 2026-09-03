"""Mímir v14.0 — 跨模型认知语义投影 (spec 阶段四任务3).

适配不同模型窗口与输出格式，实现小模型挂载优质技能后的越级
能力爆发。同一个检索装配面（QueryKernel.search 的 results 行：
fact_id/content/summary/fact_type 同名字段），按目标模型档位
投影成该模型的注入块：

- 档位表 MODEL_TIERS：claude（大窗，全保真 markdown）/ deepseek
  （中窗，结构化列表）/ local-small（小窗，紧凑 KEY: value 方言）。
  预算阶梯 claude > deepseek > local-small。
- 分层裁剪是全局策略，档位只调节力度：
  L3（iron_rule/user_pref/skill）content 全保真 —— 预算永远给
  L3 让位，与锚通道（ANCHOR_FACT_TYPES）同一铁律：小模型窗口
  再紧，挂载的技能与铁律一个字不丢，这是「越级能力爆发」的
  全部来源；
  L2（pattern）按档位降级 —— claude 全文 / deepseek 摘要 /
  local-small 硬截断；
  L1（事件与配置类）所有档只留类型+溯源行，不占预算。
- 预算守卫：超预算时从尾部丢 L1 → L2，永不丢 L3。

工程四严律：TDD 先行（tests/test_p34_cross_model.py）/
纯函数无事件流副作用 / 不落盘（无路径）/ 全量回归门禁。
"""

from __future__ import annotations

from typing import Any

#: L3 保真集合与 query.ANCHOR_FACT_TYPES / LAYER3_FACT_TYPES 同一
#: 身份 —— 投影层不得私自增删，改一处三处同步（p28/p32 pin 测试）。
L3_FACT_TYPES = ("iron_rule", "user_pref", "skill")
L2_FACT_TYPES = ("pattern",)
#: 其余 fact_type 全部按 L1 对待（事件/配置/临时/学习/参考）。

#: v14.0 模型档位表：窗口预算（token 近似）+ 输出方言。
#: token 估算用保守的「2 字符 ≈ 1 token」——中文场景宁可高估。
MODEL_TIERS: dict[str, dict[str, Any]] = {
    "claude": {
        "max_tokens": 8_000,
        "dialect": "markdown",
        "description": "large-window frontier model — full-fidelity markdown",
    },
    "deepseek": {
        "max_tokens": 3_000,
        "dialect": "structured",
        "description": "mid-window model — structured single-line records",
    },
    "local-small": {
        "max_tokens": 1_200,
        "dialect": "compact",
        "description": "small local model — minimal KEY: value lines",
    },
}

#: local-small 档 L2 的硬截断宽度（字符）——样本 pattern 的教训
#: 「先看慢查询再查连接泄漏」藏在 40 字符摘要的后半段，紧凑档
#: 必须切掉：先牺牲 L2，再谈其他。
COMPACT_L2_MAX_CHARS = 12


class ProjectionError(RuntimeError):
    """Unknown tier / malformed fact row — Fail-Closed on anything
    the projector cannot render faithfully."""


def summarize_for_tier(text: str, *, max_chars: int) -> str:
    """Hard-truncate to `max_chars` code points, ellipsis marks the cut.

    Short content passes through untouched (no information loss
    where none is forced).
    """
    if max_chars <= 0:
        raise ProjectionError("max_chars must be positive")
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _layer_of(fact_type: str) -> int:
    if fact_type in L3_FACT_TYPES:
        return 3
    if fact_type in L2_FACT_TYPES:
        return 2
    return 1


def _required(fact: dict, key: str) -> Any:
    value = fact.get(key)
    if value is None:
        raise ProjectionError(f"fact row is missing {key!r}")
    return value


def _l1_trace_line(fact: dict, dialect: str) -> str:
    """L1 only ever yields a type + provenance line — evidence stays
    traceable (fact_id is the pointer) without spending window budget
    on event prose."""
    fact_type = _required(fact, "fact_type")
    fact_id = _required(fact, "fact_id")
    if dialect == "markdown":
        return f"### {fact_type.upper()} TRACE\nfact_id: {fact_id}"
    if dialect == "structured":
        return f"[{fact_type}] trace={fact_id}"
    return f"{fact_type}! {fact_id}"


def _render(fact: dict, dialect: str) -> str:
    """Render one fact into the tier's dialect, honoring the layer
    policy: L3 verbatim, L2 tier-graded, L1 provenance-only."""
    fact_type = _required(fact, "fact_type")
    layer = _layer_of(fact_type)
    if layer == 3:
        body = str(_required(fact, "content"))
        if dialect == "markdown":
            return f"### {fact_type.upper()}\n{body}"
        if dialect == "structured":
            return f"[{fact_type}] {body}"
        return f"{fact_type}: {body}"
    if layer == 2:
        # tier-graded degradation: full content → summary → hard cut
        if dialect == "markdown":
            body = str(_required(fact, "content"))
            return f"### {fact_type.upper()}\n{body}"
        summary = str(_required(fact, "summary"))
        if dialect == "structured":
            return f"[{fact_type}] {summary}"
        return (
            f"{fact_type}: "
            + summarize_for_tier(summary, max_chars=COMPACT_L2_MAX_CHARS)
        )
    return _l1_trace_line(fact, dialect)


def _estimated_tokens(blocks: list[dict]) -> int:
    # 2 characters ≈ 1 token is deliberately conservative for CJK
    # text — an over-estimate trims earlier, which is the safe side.
    return sum(max(1, (len(str(b["text"])) + 1) // 2) for b in blocks)


def project_context(facts: list[dict], tier: str) -> dict:
    """Project retrieved facts into the injection blocks for one
    model tier.

    `facts` are canonical fact rows (get_fact / search results share
    the fact_id/content/summary/fact_type field names). Returns
    {"tier", "dialect", "budget", "estimated_tokens", "blocks"} where
    each block carries {"fact_id", "layer", "fact_type", "text"}.
    """
    if tier not in MODEL_TIERS:
        raise ProjectionError(f"unknown model tier: {tier!r}")
    if not isinstance(facts, list):
        raise ProjectionError("facts must be a list of fact rows")
    spec = MODEL_TIERS[tier]
    # Layer-first ordering: L3 leads the window, then L2, then L1 —
    # within a layer the caller's ordering (relevance) is preserved.
    decorated = sorted(
        ((_layer_of(_required(f, "fact_type")), i, f)
         for i, f in enumerate(facts)),
        key=lambda item: (-item[0], item[1]),
    )
    blocks = [
        {
            "fact_id": f["fact_id"],
            "layer": layer,
            "fact_type": f["fact_type"],
            "text": _render(f, spec["dialect"]),
        }
        for layer, _, f in decorated
    ]
    budget = spec["max_tokens"]
    # Budget guard: drop from the tail — L1 first, then L2. L3 blocks
    # are never dropped: the anchor-channel guarantee survives the
    # projection (a small model must still receive its mounted skills
    # and iron rules in full — that is the whole point of this unit).
    drop = len(blocks) - 1
    while _estimated_tokens(blocks) > budget and drop >= 0:
        if blocks[drop]["layer"] == 3:
            drop -= 1
            continue
        blocks.pop(drop)
        drop -= 1
    return {
        "tier": tier,
        "dialect": spec["dialect"],
        "budget": budget,
        "estimated_tokens": _estimated_tokens(blocks),
        "blocks": blocks,
    }
