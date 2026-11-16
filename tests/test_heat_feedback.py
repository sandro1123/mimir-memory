# -*- coding: utf-8 -*-
"""1.8.0 heat feedback: search_feedback now feeds usage/spacing state.

The production `search_feedback` table was previously write-only; 1.8.0 wires
it into the facts heat columns so decay/ranking can consume real usage.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from mimir_v8.evolve import EvolveMemService
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _store(tmp_path):
    return CanonicalStore(tmp_path / "canonical.db")


def _fact(store, content="hot fact"):
    return store.create_fact(CreateFact(
        content=content, summary="s", owner_principal="mentor", domain="system",
        fact_type="event", visibility="owner_only", sensitivity="internal",
        egress_policy="local_only", human_status="confirmed",
    ), actor_principal="mentor")["fact_id"]


def _heat(store, fid):
    row = store.get_fact(fid)
    return (row["recall_count"], row["useful_count"],
            row["last_recalled_at"], row["last_spaced_at"])


def test_useful_feedback_increments_usage(tmp_path):
    store = _store(tmp_path)
    fid = _fact(store)
    svc = EvolveMemService(store)
    svc.submit_feedback("q", fid, "useful", user_principal="mentor")
    recall, useful, last_recalled, _ = _heat(store, fid)
    assert recall == 1
    assert useful == 1
    assert last_recalled is not None


def test_useless_feedback_counts_recall_not_useful(tmp_path):
    store = _store(tmp_path)
    fid = _fact(store)
    EvolveMemService(store).submit_feedback("q", fid, "useless", user_principal="mentor")
    recall, useful, _, _ = _heat(store, fid)
    assert recall == 1
    assert useful == 0


def test_spacing_recorded_only_after_gap(tmp_path):
    store = _store(tmp_path)
    fid = _fact(store)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    with store.connect() as conn:
        conn.execute("UPDATE facts SET last_recalled_at=? WHERE fact_id=?", (old, fid))
        conn.commit()
    EvolveMemService(store).submit_feedback("q", fid, "useful", user_principal="mentor")
    _, _, _, last_spaced = _heat(store, fid)
    assert last_spaced is not None


def test_no_spacing_within_gap(tmp_path):
    store = _store(tmp_path)
    fid = _fact(store)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with store.connect() as conn:
        conn.execute("UPDATE facts SET last_recalled_at=? WHERE fact_id=?", (recent, fid))
        conn.commit()
    EvolveMemService(store).submit_feedback("q", fid, "useful", user_principal="mentor")
    _, _, _, last_spaced = _heat(store, fid)
    assert last_spaced is None
