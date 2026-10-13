"""召回解释的机器可读面（roadmap #12）+ 预算诚实（#14）。

1.3.0 的 `/v8/query?trace=true` 产出的是**人读**漏斗（RecallTrace）；
本模块补的是 **agent 消费的那半边**：一份定版、带 schema_version、
按声明预算封顶的对象，让调用方程序化知道「为什么命中、以什么代价
命中、超预算丢了什么」。

#14 硬约束：超预算时**禁止**静默把载荷换成 stub 还报 success。
本对象用 `budget_exceeded` + `dropped` 清单显式暴露，绝不静默。

叶子模块：不 import 其他 mimir 模块，被 query/api 反向依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 本对象的语义版本。后续变更走**加法**不改既有字段语义；
#: 破坏性改动必须升版本号（与 docs/VERSIONING.md 的 semver 条款一致）。
RECALL_EXPLANATION_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class RecallExplanation:
    """一次召回的可机读解释。

    declared_budget / used / dropped 三者构成**预算诚实**证据链：
    调用方声明预算，系统报告实际用量，超出部分**显式列出丢弃清单**，
    而不是把安静地砍掉的载荷当成成功返回。
    """

    declared_budget: int
    used: int
    dropped: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    schema_version: str = RECALL_EXPLANATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # #14 核心不变量：超预算必须能被解释。宣称超出却给不出丢弃
        # 清单，正是 claude-mem 那种「安静换掉载荷还报成功」的形态，
        # 一律不允许出厂。
        for item in self.dropped:
            if not item.get("reason"):
                raise ValueError(
                    "dropped 条目必须带 reason —— 否则无法向调用方解释丢了什么"
                )
        if self.used > self.declared_budget and not self.dropped:
            raise ValueError(
                "budget exceeded 但 dropped 为空 —— "
                "禁止静默降级：超预算必须枚举丢弃清单"
            )

    @property
    def budget_exceeded(self) -> bool:
        return self.used > self.declared_budget

    #: 丢弃原因 → 收窄建议。建议**由实际原因推导**，不是固定文案：
    #: 报"丢了 5 条 ACL 拦截的"和报"丢了 5 条状态不符的"，调用方能做
    #: 的事完全不同（前者要换 principal/roles，后者要放宽 status 过滤）。
    _ADVICE_BY_REASON = {
        "acl": "放宽调用方权限或换 principal/roles —— 被 ACL 拦下的条目不属于当前主体",
        "status": "用 include_provisional=true 纳入 provisional，或确认目标事实未被 tombstone",
        "layer": "调 depth=deep 以纳入 L1 原子事实（standard 档刻意跳过它们）",
        "missing": "部分候选在 canonical 已不存在（投影与真源漂移），重跑投影器",
        "budget_exceeded": "缩小 limit，或加 domain/fact_type/owner_principal 过滤收窄候选池",
    }

    def narrowing_advice(self) -> list[str]:
        """#14：超预算时给**可执行**的收窄建议。

        在预算内则无建议（空列表）——不制造无谓噪音。
        建议按 dropped 里的实际 reason 逐条推导，去重且保序。
        """
        if not self.budget_exceeded:
            return []
        advice: list[str] = []
        for item in self.dropped:
            reason = str(item.get("reason", ""))
            # reason 形如 "filtered:acl" 或 "budget_exceeded"
            key = reason.split(":", 1)[1] if ":" in reason else reason
            text = self._ADVICE_BY_REASON.get(key)
            if text and text not in advice:
                advice.append(text)
        # 兜底：超预算但 dropped 里的 reason 都不认识时，仍须给出动作
        if not advice:
            advice.append(self._ADVICE_BY_REASON["budget_exceeded"])
        return advice

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "budget": {
                "declared": self.declared_budget,
                "used": self.used,
                "exceeded": self.budget_exceeded,
                "dropped": [dict(item) for item in self.dropped],
                "narrowing_advice": self.narrowing_advice(),
            },
            "facts": [dict(item) for item in self.facts],
        }
