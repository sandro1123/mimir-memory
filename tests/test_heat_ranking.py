# -*- coding: utf-8 -*-
"""1.8.0 heat ranking: usage/spacing boost inside decay factor, capped."""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

from mimir_v8.query import QueryKernel, QueryRequest


def _now():
    return datetime.now(timezone.utc)


def _recent():
    return (_now() - timedelta(days=1)).isoformat()


def test_heat_neutral_when_unused():
    base = QueryKernel._decay_factor("L3_event", None, _recent())
    heated = QueryKernel._decay_factor(
        "L3_event", None, _recent(),
        recall_count=0, useful_count=0, last_recalled_at=None, last_spaced_at=None,
    )
    assert math.isclose(heated, base, rel_tol=1e-9)


def test_heat_boosts_with_usage_and_spacing():
    base = QueryKernel._decay_factor("L3_event", None, _recent())
    heated = QueryKernel._decay_factor(
        "L3_event", None, _recent(),
        recall_count=20, useful_count=10,
        last_recalled_at=(_now() - timedelta(hours=2)).isoformat(),
        last_spaced_at=(_now() - timedelta(days=3)).isoformat(),
    )
    assert heated > base


def test_heat_boost_capped():
    base = QueryKernel._decay_factor("L3_event", None, _recent())
    heated = QueryKernel._decay_factor(
        "L3_event", None, _recent(),
        recall_count=10**6, useful_count=10**6,
        last_recalled_at=_now().isoformat(),
        last_spaced_at=_now().isoformat(),
    )
    assert heated <= base * 1.8 + 1e-9


def test_l0_never_respects_heat_cap():
    # L0 base factor is 1.0 (no ebbinghaus decay); heat must still cap at 1.8.
    heated = QueryKernel._decay_factor(
        "L0_never", None, _recent(),
        recall_count=10**6, useful_count=10**6,
        last_recalled_at=_now().isoformat(),
        last_spaced_at=_now().isoformat(),
    )
    assert heated <= 1.8 + 1e-9
