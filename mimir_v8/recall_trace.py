"""RECALL funnel trace value objects (1.3.0 item 3-1).

Leaf module: no imports from query/api/store — it is imported BY them.
Verdict vocabulary is locked to found|not_found|degraded (spec section 5-2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

STAGE_VERDICTS = ("found", "not_found", "degraded")


@dataclass(frozen=True)
class RecallStage:
    """One funnel step: what ran, what it kept, how long it took."""
    name: str
    verdict: str
    hits: int
    elapsed_ms: float
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in STAGE_VERDICTS:
            raise ValueError(f"stage verdict must be one of {STAGE_VERDICTS}")

    def to_dict(self) -> dict:
        return {"stage": self.name, "verdict": self.verdict,
                "hits": self.hits, "elapsed_ms": self.elapsed_ms,
                "detail": dict(self.detail)}


@dataclass(frozen=True)
class RecallTrace:
    """Whole-funnel trace attached to a search() response when requested."""
    skipped: bool
    verdict: str
    degraded: bool
    lanes: dict
    stages: list

    def __post_init__(self) -> None:
        if self.verdict not in STAGE_VERDICTS:
            raise ValueError(f"trace verdict must be one of {STAGE_VERDICTS}")

    def to_dict(self) -> dict:
        return {"skipped": self.skipped, "verdict": self.verdict,
                "degraded": self.degraded, "lanes": dict(self.lanes),
                "stages": [s.to_dict() for s in self.stages]}
