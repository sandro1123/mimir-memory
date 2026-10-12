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

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "budget": {
                "declared": self.declared_budget,
                "used": self.used,
                "exceeded": self.budget_exceeded,
                "dropped": [dict(item) for item in self.dropped],
            },
            "facts": [dict(item) for item in self.facts],
        }
