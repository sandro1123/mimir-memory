# -*- coding: utf-8 -*-
"""1.8.0 heat schema migration: additive usage/spacing columns on facts.

Legacy databases (opened by a runtime that predates 1.8.0) must gain the
four heat columns on open, idempotently, without inventing usage history:
existing rows read back 0 for counters and NULL for timestamps.
"""
from __future__ import annotations

from mimir_v8.store import CanonicalStore, new_id, sha256_text, utc_now

HEAT_COLUMNS = ("recall_count", "useful_count", "last_recalled_at", "last_spaced_at")


def _columns(store: CanonicalStore) -> set[str]:
    with store.connect() as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}


def _insert_minimal_fact(store: CanonicalStore) -> str:
    now = utc_now()
    fact_id = new_id()
    content = "minimal heat fact"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO facts(
                fact_id, legacy_id, current_version, status, content, summary,
                domain, fact_type, owner_principal, project_id, visibility,
                sensitivity, egress_policy, human_status, confidence_score,
                valid_from, valid_to, recorded_at, updated_at, last_verified_at,
                tombstoned_at, decay_tier, decayed_at, schema_version, content_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fact_id, None, 1, "active", content, "minimal", "system", "event",
                "u1", None, "owner_only", "internal", "local_only", "unreviewed",
                None, None, None, now, now, None, None, "L3_event", None, 21,
                sha256_text(content),
            ),
        )
        conn.commit()
    return fact_id


def test_fresh_schema_has_heat_columns(tmp_path):
    store = CanonicalStore(tmp_path / "fresh.db")
    assert set(HEAT_COLUMNS) <= _columns(store)


def test_heat_columns_default_null_or_zero(tmp_path):
    store = CanonicalStore(tmp_path / "fresh.db")
    fid = _insert_minimal_fact(store)
    row = store.get_fact(fid)
    assert row["recall_count"] == 0
    assert row["useful_count"] == 0
    assert row["last_recalled_at"] is None
    assert row["last_spaced_at"] is None


def test_legacy_db_gains_columns_on_open(tmp_path):
    db = tmp_path / "legacy.db"
    store = CanonicalStore(db)
    fid = _insert_minimal_fact(store)
    with store.connect() as conn:
        for name in HEAT_COLUMNS:
            conn.execute(f"ALTER TABLE facts DROP COLUMN {name}")
        conn.commit()
    assert not (set(HEAT_COLUMNS) & _columns(store))

    reopened = CanonicalStore(db)  # triggers additive migration on open
    assert set(HEAT_COLUMNS) <= _columns(reopened)
    row = reopened.get_fact(fid)
    assert row["recall_count"] == 0
    assert row["useful_count"] == 0
    assert row["last_recalled_at"] is None
    assert row["last_spaced_at"] is None


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "rerun.db"
    first = CanonicalStore(db)
    fid = _insert_minimal_fact(first)
    del first
    second = CanonicalStore(db)  # second open must not raise or duplicate columns
    assert set(HEAT_COLUMNS) <= _columns(second)
    row = second.get_fact(fid)
    assert row["recall_count"] == 0
