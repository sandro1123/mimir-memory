"""v14.1.0 P0 golden-set stewardship tests (TDD).

Two production findings from the 2026-09-04 re-test drive this file:

1. The hit_rate@3 floor has zero headroom because the golden facts
   themselves aged — unreviewed, zero confidence, L4 weakest decay
   tier, crowded out by fresh user_prefs. That is fact-layer aging,
   not a ranking bug, so the fix is stewardship: a health sentinel
   that reports golden-fact staleness before floors erode, and an
   upgrade path (human review + tier promotion) for the stale facts.

2. ``UpdateFact`` cannot change ``decay_tier`` at all, so the L4 →
   L1 promotion the fix needs is impossible through the governed
   event-sourced path. The command must learn the field.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.eval_suite import GOLDEN_SET, golden_health, main
from mimir_v8.schema import DECAY_TIERS, CreateFact, UpdateFact, ValidationError
from mimir_v8.store import CanonicalStore


def _fact(
    store,
    content,
    *,
    owner="mentor",
    fact_type="pattern",
    human_status="confirmed",
    confidence_score=0.8,
    visibility="all",
):
    """Create a fact. decay_tier is derived from fact_type by
    DECAY_TIER_MAP (pattern → L4) — an override therefore seeds via
    UpdateFact, which is the governed promotion path under test."""
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain="knowledge",
            fact_type=fact_type,
            visibility=visibility,
            sensitivity="internal",
            egress_policy="local_only",
            human_status=human_status,
            confidence_score=confidence_score,
        ),
        actor_principal=owner,
    )["fact_id"]


def _set_tier(store, fact_id, tier, *, actor="mentor"):
    current = store.get_fact(fact_id)
    store.update_fact(
        UpdateFact(
            fact_id=fact_id,
            expected_version=current["current_version"],
            decay_tier=tier,
            change_reason="stewardship test tier override",
        ),
        actor_principal=actor,
    )


def _seed_golden_store(path, overrides=None):
    """Build a canonical store where every GOLDEN_SET pinned id exists
    as a healthy fact (active / confirmed / 0.8 confidence / L1 tier).

    Rows go in through a raw INSERT on the initialized store: the
    golden ids are pinned constants, and create_fact mints fresh ids
    while UPDATE-ing a live fact_id trips the fact_versions foreign
    key. The sentinel under test is a pure reader — what matters is
    the row shape, not the write path. Per-fact overrides arrive as
    (index -> column-value) so a test can age exactly one.
    """
    store = CanonicalStore(path)
    now = "2026-09-04T00:00:00+00:00"
    with store.transaction() as connection:
        for i, (_q, golden_id, _marker) in enumerate(GOLDEN_SET):
            columns = {
                "human_status": "confirmed",
                "confidence_score": 0.8,
                "decay_tier": "L1_preference",
                "status": "active",
            }
            columns.update((overrides or {}).get(i, {}))
            connection.execute(
                """INSERT INTO facts(
                    fact_id, current_version, status, content, summary,
                    domain, fact_type, owner_principal, visibility,
                    sensitivity, egress_policy, human_status,
                    confidence_score, recorded_at, updated_at,
                    decay_tier, schema_version, content_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    golden_id, 1, columns["status"],
                    f"golden fact {i} marker-{i}",
                    f"golden fact {i} marker-{i}"[:40],
                    "knowledge", "pattern", "mentor", "all",
                    "internal", "local_only", columns["human_status"],
                    columns["confidence_score"], now, now,
                    columns["decay_tier"], 20, f"hash-{golden_id[:8]}",
                ),
            )
    return store, [golden_id for _q, golden_id, _m in GOLDEN_SET]


class GoldenStewardshipTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "canonical.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _connection(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


# ── UpdateFact learns decay_tier ─────────────────────────────


class TestUpdateFactDecayTier(GoldenStewardshipTestCase):
    def test_update_fact_changes_decay_tier(self):
        store, ids = _seed_golden_store(self.path)
        before = store.get_fact(ids[0])
        # fixture baseline lands at L1_preference; demote exercises
        # the channel, and the event-sourced test below promotes.
        self.assertEqual(before["decay_tier"], "L1_preference")

        result = store.update_fact(
            UpdateFact(
                fact_id=ids[0],
                expected_version=before["current_version"],
                decay_tier="L4_temporary",
                change_reason="demote to temporary",
            ),
            actor_principal="mentor",
        )
        after = store.get_fact(ids[0])
        self.assertEqual(after["decay_tier"], "L4_temporary")
        self.assertEqual(after["current_version"], result["version"])
        self.assertEqual(result["version"], before["current_version"] + 1)

    def test_update_fact_rejects_unknown_tier(self):
        store, ids = _seed_golden_store(self.path)
        current = store.get_fact(ids[0])
        with self.assertRaises(ValidationError):
            store.update_fact(
                UpdateFact(
                    fact_id=ids[0],
                    expected_version=current["current_version"],
                    decay_tier="L9_bogus",
                ),
                actor_principal="mentor",
            )

    def test_decay_tier_change_is_event_sourced(self):
        store, ids = _seed_golden_store(self.path)
        current = store.get_fact(ids[0])
        store.update_fact(
            UpdateFact(
                fact_id=ids[0],
                expected_version=current["current_version"],
                decay_tier="L0_never",
                change_reason="promote to never-decay",
            ),
            actor_principal="mentor",
        )
        with store.transaction() as connection:
            events = connection.execute(
                """SELECT event_type, payload_json FROM memory_events
                WHERE aggregate_id=? ORDER BY event_seq DESC LIMIT 1""",
                (ids[0],),
            ).fetchone()
        self.assertEqual(events["event_type"], "fact.updated")
        payload = json.loads(events["payload_json"])
        self.assertIn("decay_tier", payload["changed_fields"])

    def test_no_change_conflict_includes_decay_tier(self):
        # An update that only re-states the current tier must still
        # conflict — the field participates in the no-op guard.
        store, ids = _seed_golden_store(self.path)
        current = store.get_fact(ids[0])
        with self.assertRaises(Exception):
            store.update_fact(
                UpdateFact(
                    fact_id=ids[0],
                    expected_version=current["current_version"],
                    decay_tier=current["decay_tier"],
                ),
                actor_principal="mentor",
            )

    def test_tier_choices_are_the_schema_set(self):
        self.assertEqual(
            {tier for tier in DECAY_TIERS},
            {"L0_never", "L1_preference", "L2_config", "L3_event",
             "L4_temporary", "L5_ephemeral"},
        )


# ── golden_health sentinel ────────────────────────────────────


class TestGoldenHealth(GoldenStewardshipTestCase):
    def test_all_healthy_golden_set(self):
        _seed_golden_store(self.path)
        with self._connection() as connection:
            report = golden_health(connection)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["n_cases"], len(GOLDEN_SET))
        self.assertEqual(report["unhealthy"], [])

    def test_unreviewed_fact_is_flagged(self):
        store, ids = _seed_golden_store(self.path, {0: {"human_status": "unreviewed"}})
        with self._connection() as connection:
            report = golden_health(connection)
        self.assertFalse(report["healthy"])
        case = report["unhealthy"][0]
        self.assertEqual(case["fact_id"], ids[0])
        self.assertIn("unconfirmed", case["flags"])

    def test_zero_confidence_is_flagged(self):
        store, ids = _seed_golden_store(
            self.path, {1: {"confidence_score": None}}
        )
        with self._connection() as connection:
            report = golden_health(connection)
        self.assertFalse(report["healthy"])
        case = report["unhealthy"][0]
        self.assertEqual(case["fact_id"], ids[1])
        self.assertIn("low_confidence", case["flags"])

    def test_weak_decay_tier_is_flagged(self):
        store, ids = _seed_golden_store(
            self.path, {2: {"decay_tier": "L4_temporary"}}
        )
        with self._connection() as connection:
            report = golden_health(connection)
        self.assertFalse(report["healthy"])
        case = report["unhealthy"][0]
        self.assertEqual(case["fact_id"], ids[2])
        self.assertIn("weak_tier", case["flags"])

    def test_missing_fact_is_flagged(self):
        # An initialized store with zero facts: no row carries the
        # pinned golden ids, so every case reports missing — the
        # loudest staleness form (the marker fallback would save the
        # benchmark run, but the stewardship sentinel must not be
        # fooled into green).
        CanonicalStore(self.path)
        with self._connection() as connection:
            report = golden_health(connection)
        self.assertFalse(report["healthy"])
        self.assertEqual(len(report["unhealthy"]), len(GOLDEN_SET))
        for case in report["unhealthy"]:
            self.assertIn("missing", case["flags"])

    def test_tombstoned_fact_is_flagged(self):
        store, ids = _seed_golden_store(
            self.path, {3: {"status": "tombstoned"}}
        )
        with self._connection() as connection:
            report = golden_health(connection)
        case = report["unhealthy"][0]
        self.assertEqual(case["fact_id"], ids[3])
        self.assertIn("inactive", case["flags"])

    def test_flags_can_stack(self):
        store, ids = _seed_golden_store(
            self.path,
            {4: {"human_status": "unreviewed", "decay_tier": "L5_ephemeral"}},
        )
        with self._connection() as connection:
            report = golden_health(connection)
        case = report["unhealthy"][0]
        self.assertEqual(case["fact_id"], ids[4])
        self.assertIn("unconfirmed", case["flags"])
        self.assertIn("weak_tier", case["flags"])


class TestGoldenHealthCLI(GoldenStewardshipTestCase):
    def test_healthy_exit_zero(self):
        _seed_golden_store(self.path)
        report = main(["--golden-health", "--db", str(self.path)])
        self.assertTrue(report["healthy"])

    def test_unhealthy_exit_four(self):
        _seed_golden_store(self.path, {0: {"human_status": "unreviewed"}})
        with self.assertRaises(SystemExit) as ctx:
            main(["--golden-health", "--db", str(self.path)])
        self.assertEqual(ctx.exception.code, 4)

    def test_missing_db_exits_two(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--golden-health", "--db", str(self.root / "nope.db")])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
