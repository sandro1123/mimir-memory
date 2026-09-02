"""Mímir v12 Insight — M1a/M1b tests.

Verify:
- DECAY_HALF_LIFE has L5_ephemeral = 7 (M1a)
- ephemeral fact_type maps to L5_ephemeral (M1a)
- _decay_factor applies Ebbinghaus decay per tier (M1a)
- _decay_factor deweights expired facts by 50% via valid_to (M1b Chronos)
- _decay_factor returns 1.0 for L0_never (identity lane, zero decay)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import DECAY_HALF_LIFE, DECAY_TIER_MAP, DECAY_TIERS, CreateFact
from mimir_v8.store import CanonicalStore


def _make_fact(store, content, *, fact_type="event", confidence=0.5,
               valid_from=None, valid_to=None, domain="knowledge"):
    result = store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal="mentor",
            domain=domain,
            fact_type=fact_type,
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status="confirmed",
            confidence_score=confidence,
            valid_from=valid_from,
            valid_to=valid_to,
        ),
        actor_principal="mentor",
    )
    return result["fact_id"]


class _FunnelFixture:
    """Store + FTS projector populated deterministically for trace tests."""

    def __init__(self, tmp):
        self.store = CanonicalStore(Path(tmp) / "canonical.db")
        self.fts = FTSProjector(Path(tmp) / "fts.db")
        self.runner = ProjectorRunner(self.store, self.fts)
        self.kernel = QueryKernel(
            self.store, fts=self.fts,
            vector=None, graph=None, embedder=None,
        )

    def seed(self, content, **kw):
        fact_id = _make_fact(self.store, content, **kw)
        return fact_id

    def project(self):
        self.runner.run_once(limit=500)
        return self.fts.count()

    def trace(self, text, **kw):
        threshold = kw.pop("dedup_threshold", 0.8)
        return self.kernel.trace(QueryRequest(
            text=text, principal_id="mentor", roles=(),
            is_admin=False, owner_principal=None, domain=None, fact_type=None,
            use_vector=False, use_fts=True, use_graph=False,
            include_provisional=False, **kw,
        ), dedup_threshold=threshold)


class TestM1aDecayTiers(unittest.TestCase):
    """M1a: Ebbinghaus decay model with ephemeral lane."""

    def test_l5_ephemeral_tier_exists(self):
        self.assertIn("L5_ephemeral", DECAY_TIERS)

    def test_l5_ephemeral_half_life_is_7(self):
        self.assertEqual(DECAY_HALF_LIFE.get("L5_ephemeral"), 7)

    def test_ephemeral_fact_type_maps_to_l5(self):
        self.assertEqual(DECAY_TIER_MAP.get("ephemeral"), "L5_ephemeral")

    def test_l0_never_has_no_half_life(self):
        self.assertIsNone(DECAY_HALF_LIFE.get("L0_never"))

    def test_tier_ordering(self):
        # ephemeral must decay faster than temporary
        self.assertLess(DECAY_HALF_LIFE["L5_ephemeral"], DECAY_HALF_LIFE["L4_temporary"])


class TestM1aEbbinghausFactor(unittest.TestCase):
    """M1a: Ebbinghaus decay factor formula."""

    def _factor(self, tier, valid_to=None, updated_at="2026-01-01T00:00:00+00:00"):
        return QueryKernel._decay_factor(tier, valid_to, updated_at)

    def test_l0_never_no_decay(self):
        self.assertEqual(self._factor("L0_never"), 1.0)

    def test_ephemeral_decays_faster_than_standard(self):
        # recent update (1 day ago) — ephemeral (7d) vs standard (90d)
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        ephem = self._factor("L5_ephemeral", updated_at=recent)
        standard = self._factor("L2_config", updated_at=recent)
        self.assertLess(ephem, standard)

    def test_decay_bounded_below(self):
        # very old fact should floor at 0.01, never 0.0 (deweight, not delete)
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()
        f = self._factor("L2_config", updated_at=old)
        self.assertGreaterEqual(f, 0.01)


class TestM1bChronosValidTo(unittest.TestCase):
    """M1b: Chronos double-timeline deweights expired facts by 50%."""

    def test_expired_valid_to_deweights_half(self):
        from datetime import datetime, timezone, timedelta
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        # L0_never with valid_to — baseline 1.0, expired → 0.5
        self.assertAlmostEqual(
            QueryKernel._decay_factor("L0_never", expired, "2026-01-01T00:00:00+00:00"),
            0.5, places=2)

    def test_future_valid_to_no_deweight(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        self.assertEqual(
            QueryKernel._decay_factor("L0_never", future, "2026-01-01T00:00:00+00:00"),
            1.0)

    def test_null_valid_to_no_deweight(self):
        self.assertEqual(
            QueryKernel._decay_factor("L0_never", None, "2026-01-01T00:00:00+00:00"),
            1.0)

    def test_fresh_db_has_valid_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with store.connect() as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
            self.assertIn("valid_from", cols)
            self.assertIn("valid_to", cols)


class TestM1bChronosValidFrom(unittest.TestCase):
    """M1b: facts with a future valid_from are ranked last."""

    @staticmethod
    def _not_yet(valid_from):
        return QueryKernel._not_yet_effective(valid_from)

    def test_future_valid_from_not_yet_effective(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        self.assertTrue(self._not_yet(future))

    def test_null_valid_from_effective(self):
        self.assertFalse(self._not_yet(None))

    def test_past_valid_from_effective(self):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        self.assertFalse(self._not_yet(past))

    def test_now_valid_from_effective(self):
        from datetime import datetime, timezone
        self.assertFalse(self._not_yet(datetime.now(timezone.utc).isoformat()))

    def test_invalid_valid_from_defaults_effective(self):
        self.assertFalse(self._not_yet("not-a-date"))


class TestM1aEphemeralFactPersistence(unittest.TestCase):
    """M1a: ephemeral facts persist with the L5_ephemeral decay tier."""

    def _make(self, tmp, fact_type="ephemeral"):
        store = CanonicalStore(Path(tmp) / "canonical.db")
        result = store.create_fact(
            CreateFact(
                content="temporary observation",
                summary="temp",
                owner_principal="mentor",
                domain="system",
                fact_type=fact_type,
                visibility="all",
                sensitivity="internal",
                egress_policy="local_only",
                human_status="unreviewed",
            ),
            actor_principal="mentor",
        )
        return store, result["fact_id"]

    def test_ephemeral_fact_created_with_l5_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, fact_id = self._make(tmp)
            fact = store.get_fact(fact_id)
            self.assertEqual(fact["decay_tier"], "L5_ephemeral")

    def test_ephemeral_fact_does_not_violate_check_constraint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, fact_id = self._make(tmp)
            self.assertTrue(store.get_fact(fact_id)["fact_id"])


class TestM1cEvolveMemCycle(unittest.TestCase):
    """M1c: full EvolveMem feedback → report → evolve cycle adjusts confidence."""

    def test_feedback_then_evolve_nudges_confidence(self):
        from mimir_v8.evolve import EvolveMemService
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            result = store.create_fact(
                CreateFact(
                    content="N100 cooling facts", summary="n100",
                    owner_principal="mentor", domain="infrastructure",
                    fact_type="pattern", visibility="all", sensitivity="internal",
                    egress_policy="local_only", human_status="confirmed",
                    confidence_score=0.5,
                ), actor_principal="mentor",
            )
            fid = result["fact_id"]
            svc = EvolveMemService(store)
            for _ in range(5):
                svc.submit_feedback("n100 cooling", fid, "useful")
            report = svc.report()
            self.assertEqual(report["signals"]["useful"], 5)
            outcome = svc.evolve()
            self.assertEqual(outcome["adjusted"], 1)
            self.assertAlmostEqual(store.get_fact(fid)["confidence_score"], 0.55, places=2)

    def test_useless_signals_nudge_negative(self):
        from mimir_v8.evolve import EvolveMemService
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            result = store.create_fact(
                CreateFact(
                    content="stale guidance", summary="stale",
                    owner_principal="mentor", domain="system",
                    fact_type="pattern", visibility="all", sensitivity="internal",
                    egress_policy="local_only", human_status="confirmed",
                    confidence_score=0.5,
                ), actor_principal="mentor",
            )
            fid = result["fact_id"]
            svc = EvolveMemService(store)
            for _ in range(5):
                svc.submit_feedback("stale guidance", fid, "useless")
            outcome = svc.evolve()
            self.assertEqual(outcome["adjusted"], 1)
            self.assertAlmostEqual(store.get_fact(fid)["confidence_score"], 0.45, places=2)

    def test_invalid_signal_rejected(self):
        from mimir_v8.evolve import EvolveMemService
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with self.assertRaises(ValueError):
                EvolveMemService(store).submit_feedback("q", "f", "bogus")

    def test_fresh_db_has_v15_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with store.connect() as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertIn("search_feedback", tables)
            self.assertIn("quality_metrics", tables)

    def test_evolve_writes_memory_event_audit(self):
        from mimir_v8.evolve import EvolveMemService
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            result = store.create_fact(
                CreateFact(
                    content="audit target", summary="audit",
                    owner_principal="mentor", domain="system",
                    fact_type="pattern", visibility="all", sensitivity="internal",
                    egress_policy="local_only", human_status="confirmed",
                    confidence_score=0.5,
                ), actor_principal="mentor",
            )
            fid = result["fact_id"]
            svc = EvolveMemService(store)
            for _ in range(5):
                svc.submit_feedback("audit target", fid, "useful")
            svc.evolve()
            with store.connect() as conn:
                events = conn.execute(
                    "SELECT COUNT(*) FROM memory_events WHERE event_type='fact.evolved'"
                ).fetchone()[0]
            self.assertGreaterEqual(events, 1)


class TestM2aRecallFunnel(unittest.TestCase):
    """M2a: 5-stage recall funnel trace with per-stage timing/hits/decay."""

    def _fx(self):
        return _FunnelFixture(tempfile.mkdtemp())

    def test_trace_returns_all_five_stages_in_order(self):
        fx = self._fx()
        fx.seed("canonical store keeps agent facts")
        fx.project()
        data = fx.trace("canonical store facts")
        names = [s["stage"] for s in data["stages"]]
        # v12.2.0: AnchorChannel sits between pool assembly and dedup;
        # LayerSweep (L2/L1 progressive assembly) follows the anchors.
        self.assertEqual(names, ["RelevanceGate", "CandidatePool",
                                 "AnchorChannel", "LayerSweep",
                                 "JaccardDedup", "ChronosDecay", "TopK"])
        for s in data["stages"]:
            self.assertIn("elapsed_ms", s)
            self.assertIn("hit", s)
            self.assertIn("dropped", s)

    def test_candidate_pool_hydrates_only_searchable_facts(self):
        fx = self._fx()
        fx.seed("alpha theta canonical store")
        fx.seed("beta gamma unrelated topic entirely")
        fx.project()
        self.assertEqual(fx.fts.count(), 2)
        data = fx.trace("canonical store alpha theta")
        pool = [s for s in data["stages"] if s["stage"] == "CandidatePool"][0]
        self.assertEqual(pool["total"], 1)
        self.assertEqual(pool["hit"], 1)
        self.assertIn("channels", pool)

    def test_jaccard_dedup_drops_near_duplicates(self):
        fx = self._fx()
        fx.seed("agent stores facts in canonical store for memory recall system")
        fx.seed("agent stores facts in canonical store for memory recall")
        fx.seed("delta omega unrelated")  # not matched by query
        fx.project()
        # query matches both near-duplicates → dedup should drop one
        # (depth=deep: event facts are L1, standard assembly would gate them
        #  before the funnel under test here is reached)
        data = fx.trace("agent stores facts in canonical memory store",
                        dedup_threshold=0.6, depth="deep")
        dedup = [s for s in data["stages"] if s["stage"] == "JaccardDedup"][0]
        self.assertGreaterEqual(dedup["total"], 2)
        self.assertLess(dedup["hit"], dedup["total"])

    def test_low_jaccard_threshold_keeps_more(self):
        fx = self._fx()
        fx.seed("agent facts live in the canonical memory store")
        fx.seed("agent stores facts in canonical store for memory")
        fx.project()
        lax = fx.trace("agent stores facts in canonical memory store", dedup_threshold=0.2)
        strict = fx.trace("agent stores facts in canonical memory store", dedup_threshold=0.95)
        lax_hits = [s for s in lax["stages"] if s["stage"] == "JaccardDedup"][0]["hit"]
        strict_hits = [s for s in strict["stages"] if s["stage"] == "JaccardDedup"][0]["hit"]
        self.assertGreaterEqual(strict_hits, lax_hits)

    def test_evolve_report_api_has_7day_window(self):
        from mimir_v8.evolve import EvolveMemService
        import tempfile as _tf
        store = CanonicalStore(Path(_tf.mkdtemp()) / "canonical.db")
        svc = EvolveMemService(store)
        report = svc.report()
        for key in ("window_days", "signals", "total_feedback", "recent_cycles"):
            self.assertIn(key, report)
        self.assertEqual(report["window_days"], 7)


class TestM2bQualityBoard(unittest.TestCase):
    """M2b: quality board data — signals split, quality_metrics rows, evolution audit."""

    def _cycle(self):
        """Seed feedback for two facts, run one evolve cycle, return (store, data)."""
        import tempfile as _tf
        from mimir_v8.evolve import EvolveMemService
        store = CanonicalStore(Path(_tf.mkdtemp()) / "canonical.db")
        svc = EvolveMemService(store)
        fids = []
        for content in ("quality board first target", "quality board second target"):
            fids.append(_make_fact(store, content, fact_type="pattern"))
        # Each fact needs >= MIN_SIGNALS_PER_FACT (5) total signals to be adjusted.
        # fid[0]: 4 useful + 1 useless -> ratio 0.8 >= 0.6 -> +delta
        for _ in range(4):
            svc.submit_feedback("quality board", fids[0], "useful")
        svc.submit_feedback("quality board", fids[0], "useless")
        # fid[1]: 1 useful + 4 useless -> ratio 0.2 <= 0.4 -> -delta
        svc.submit_feedback("quality board", fids[1], "useful")
        for _ in range(4):
            svc.submit_feedback("quality board", fids[1], "useless")
        data = svc.evolve()
        return store, fids, data

    def test_signals_split_tracks_useful_useless_correction(self):
        from mimir_v8.evolve import EvolveMemService
        import tempfile as _tf
        store = CanonicalStore(Path(_tf.mkdtemp()) / "canonical.db")
        svc = EvolveMemService(store)
        fid = _make_fact(store, "signal split feed", fact_type="pattern")
        svc.submit_feedback("q", fid, "useful")
        svc.submit_feedback("q", fid, "useless")
        svc.submit_feedback("q", fid, "correction")
        report = svc.report()
        self.assertEqual(report["signals"]["useful"], 1)
        self.assertEqual(report["signals"]["useless"], 1)
        self.assertEqual(report["signals"]["correction"], 1)
        self.assertEqual(report["total_feedback"], 3)

    def test_quality_metrics_row_written_per_cycle(self):
        store, fids, data = self._cycle()
        self.assertEqual(data["adjusted"], 2)
        with store.connect() as conn:
            row = conn.execute(
                "SELECT useful_signals, useless_signals, query_count "
                "FROM quality_metrics"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["useful_signals"], 1)
        self.assertGreaterEqual(row["useless_signals"], 1)

    def test_evolve_writes_audit_event_for_quality(self):
        store, fids, data = self._cycle()
        with store.connect() as conn:
            events = conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE event_type='fact.evolved'"
            ).fetchone()[0]
        self.assertEqual(events, data["adjusted"])

    def test_report_recent_cycles_lists_rows_desc(self):
        store, fids, data = self._cycle()
        from mimir_v8.evolve import EvolveMemService
        report = EvolveMemService(store).report()
        cycles = report["recent_cycles"]
        self.assertGreaterEqual(len(cycles), 1)
        self.assertIn("date", cycles[0])
        self.assertIn("evolved_at", cycles[0])

    def test_deduped_summary_via_query_kernel_present(self):
        # sanity: funnel results carry decay factor + not_yet_effective flags
        # (depth=deep: event facts are L1, gated out of standard assembly)
        fx = _FunnelFixture(tempfile.mkdtemp())
        fx.seed("agent stores facts in canonical memory store")
        fx.project()
        data = fx.trace("agent canonical memory", dedup_threshold=0.8,
                        depth="deep")
        self.assertTrue(data["results"])
        for r in data["results"]:
            self.assertIn("decay_factor", r)
            self.assertIn("not_yet_effective", r)
            self.assertIn("score", r)

    def test_trace_total_summation(self):
        # the funnel sum must remain consistent: pool ≥ dedup ≥ topK totals
        fx = _FunnelFixture(tempfile.mkdtemp())
        fx.seed("first retrieval target about agent memory")
        fx.seed("second retrieval target about agent memory store")
        fx.project()
        data = fx.trace("agent memory retrieval target", dedup_threshold=0.5)
        by = {s["stage"]: s["total"] for s in data["stages"]}
        pool, dedup = by["CandidatePool"], by["JaccardDedup"]
        self.assertGreaterEqual(pool, dedup)
        self.assertGreaterEqual(dedup, by["TopK"])


class TestM3aConflictResolution(unittest.TestCase):
    """M3a: conflict detection + resolution marks the loser disputed."""

    def _detect(self, tmp):
        from mimir_v8.conflict import ConflictService
        store = CanonicalStore(Path(tmp) / "canonical.db")
        fid_a = _make_fact(store, "agent stores facts in canonical memory store for recall")
        fid_b = _make_fact(store, "agent stores facts in canonical memory store for recall now")
        fid_c = _make_fact(store, "totally unrelated thing about gardening")
        svc = ConflictService(store)
        result = svc.detect(threshold=0.6)
        return store, svc, result, {"a": fid_a, "b": fid_b, "c": fid_c}

    def test_detect_finds_near_duplicate_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, result, fids = self._detect(tmp)
            self.assertGreaterEqual(result["created"], 1)
            conflicts = svc.list(status="open")
            self.assertTrue(conflicts)
            self.assertEqual(conflicts[0]["status"], "open")
            self.assertIn("facts", conflicts[0])

    def test_detect_skips_same_domain_unrelated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, result, fids = self._detect(tmp)
            open_list = svc.list("open")
            facts_in = {fid for c in open_list for fid in (c["fact_id_a"], c["fact_id_b"])}
            # the unrelated fact never becomes a conflict partner
            self.assertNotIn(fids["c"], facts_in)

    def test_resolve_marks_loser_disputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, result, fids = self._detect(tmp)
            conflicts = svc.list("open")
            cid = conflicts[0]["conflict_id"]
            winner = conflicts[0]["fact_id_a"]
            loser = conflicts[0]["fact_id_b"]
            outcome = svc.resolve(cid, winner, reason="newer content")
            self.assertEqual(outcome["loser_fact_id"], loser)
            self.assertEqual(store.get_fact(loser)["status"], "disputed")
            self.assertEqual(store.get_fact(winner)["status"], "active")
            # audit event written
            with store.connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM memory_events WHERE event_type='fact.conflict_lost'"
                ).fetchone()[0]
            self.assertGreaterEqual(n, 1)
            done = svc.list("resolved")
            self.assertEqual(done[0]["loser_fact_id"], loser)

    def test_resolve_rejects_unknown_winner(self):
        import mimir_v8.conflict as mod
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, result, fids = self._detect(tmp)
            cid = svc.list("open")[0]["conflict_id"]
            with self.assertRaises(mod.ConflictResolutionError):
                svc.resolve(cid, "nope-not-a-fact")

    def test_detect_idempotent_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, result, fids = self._detect(tmp)
            second = svc.detect(threshold=0.6)
            self.assertEqual(second["created"], 0)
            self.assertGreaterEqual(second["existing"], 1)

    def test_dismiss_closes_without_status_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, result, fids = self._detect(tmp)
            cid = svc.list("open")[0]["conflict_id"]
            svc.dismiss(cid, reason="false positive")
            self.assertEqual(store.get_fact(fids["a"])["status"], "active")
            self.assertEqual(store.get_fact(fids["b"])["status"], "active")
            self.assertEqual(svc.list("open"), [])
            self.assertEqual(svc.list("dismissed")[0]["reason"], "false positive")

    def test_conflict_resolution_table_created_on_fresh_db(self):
        from mimir_v8.conflict import V16_ADDITIVE_STATEMENTS
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with store.connect() as conn:
                present = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertIn("conflict_resolutions", present)
            self.assertGreater(len(V16_ADDITIVE_STATEMENTS), 0)


class TestM3bSkillCrystallization(unittest.TestCase):
    """M3b: topic clustering -> candidate -> human approve/dismiss."""

    def _seed(self, tmp, copies=3, topic="redis uptime check flow now"):
        from mimir_v8.crystallize import CrystalService
        store = CanonicalStore(Path(tmp) / "canonical.db")
        ids = []
        for i in range(copies):
            ids.append(_make_fact(
                store,
                f"{topic} variant {i}",
                fact_type="pattern",
                domain="infrastructure",
            ))
        _make_fact(store, "completely different cooking pasta", domain="knowledge")
        return store, CrystalService(store), ids

    def test_scan_creates_candidate_when_freq_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, ids = self._seed(tmp, copies=3)
            result = svc.scan()
            self.assertGreaterEqual(result["created"], 1)
            candidates = svc.list("candidate")
            self.assertTrue(candidates)
            self.assertEqual(candidates[0]["status"], "candidate")
            self.assertEqual(sorted(candidates[0]["sample_ids"]), sorted(ids))

    def test_scan_skips_below_threshold_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, ids = self._seed(tmp, copies=2)
            result = svc.scan(min_freq=3)
            self.assertEqual(result["created"], 0)
            self.assertEqual(svc.list("candidate"), [])

    def test_scan_refreshes_existing_open_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, ids = self._seed(tmp, copies=3)
            first = svc.scan()
            cid = svc.list("candidate")[0]["candidate_id"]
            second = svc.scan()
            self.assertEqual(second["created"], 0)
            self.assertGreaterEqual(second["updated"], 1)
            self.assertEqual(svc.list("candidate")[0]["candidate_id"], cid)

    def test_approved_candidate_materializes_pattern_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, ids = self._seed(tmp, copies=3)
            svc.scan()
            cid = svc.list("candidate")[0]["candidate_id"]
            outcome = svc.approve(cid, actor_principal="mentor")
            crystal_fid = outcome["crystal_fact_id"]
            self.assertTrue(crystal_fid)
            fact = store.get_fact(crystal_fid)
            self.assertEqual(fact["status"], "active")
            self.assertEqual(fact["fact_type"], "pattern")
            self.assertEqual(fact["domain"], "infrastructure")
            self.assertEqual(store.get_fact(ids[0])["status"], "active")
            with store.connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM memory_events WHERE event_type='crystal.approved'"
                ).fetchone()[0]
            self.assertGreaterEqual(n, 1)

    def test_approve_twice_rejected(self):
        import mimir_v8.crystallize as mod
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, ids = self._seed(tmp, copies=3)
            svc.scan()
            cid = svc.list("candidate")[0]["candidate_id"]
            svc.approve(cid)
            with self.assertRaisesRegex(mod.CrystalError, "already approved"):
                svc.approve(cid)

    def test_dismiss_leaves_facts_untouched(self):
        import mimir_v8.crystallize as mod
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, ids = self._seed(tmp, copies=3)
            svc.scan()
            cid = svc.list("candidate")[0]["candidate_id"]
            result = svc.dismiss(cid, reason="not a skill")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(svc.list("candidate"), [])
            self.assertEqual(svc.list("dismissed")[0]["reason"], "not a skill")
            for fid in ids:
                self.assertEqual(store.get_fact(fid)["status"], "active")

    def test_crystal_tables_created_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with store.connect() as conn:
                present = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertIn("crystal_candidates", present)


class TestM4ObsidianWikilink(unittest.TestCase):
    """M4: Obsidian Wikilink double-linking is deterministic and testable."""

    def test_note_slug_is_stable_and_unique(self):
        from mimir_v8.wikilink import note_slug
        a = note_slug("abc", "deadbeef", "N100 server handling")
        b = note_slug("abc", "deadbeef", "N100 server handling")
        c = note_slug("xyz", "cafebabe", "N100 server handling")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_forward_link_renders_wikilink(self):
        from mimir_v8.wikilink import forward_link
        link = forward_link("N100 upgrade notes")
        self.assertTrue(link.startswith("[["))
        self.assertTrue(link.endswith("]]"))
        self.assertIn("|", link)

    def test_related_links_are_double_directional(self):
        from mimir_v8.wikilink import related_links, note_slug
        facts = [
            {"fact_id": "f1", "content_hash": "aaaa", "summary": "N100 is the new server hardware"},
            {"fact_id": "f2", "content_hash": "bbbb", "summary": "deploy to N100 today"},
            {"fact_id": "f3", "content_hash": "cccc", "summary": "cooking pasta recipe"},
        ]
        targets = related_links(facts)
        self.assertEqual(len(targets), 3)
        slugs = {note_slug(f["fact_id"], f["content_hash"], f["summary"]) for f in facts}
        self.assertEqual(set(targets), slugs)
        # unrelated fact still gets its own note
        pasta = [t for t in targets if "pasta" in t]
        self.assertEqual(len(pasta), 1)

    def test_fact_note_contains_backlinks_section(self):
        from mimir_v8.wikilink import fact_note
        note = fact_note(
            {"fact_id": "f1", "content_hash": "aaaa",
             "content": "N100 server handles all requests",
             "summary": "N100 is primary server", "domain": "infrastructure",
             "fact_type": "pattern", "confidence_score": 0.9},
            related=["n100-secondary-f1"],
        )
        self.assertIn("## Backlinks", note)
        self.assertIn("[[n100-secondary-f1]]", note)
        self.assertIn("type: mimir-fact", note)

    def test_deep_reader_log_requires_wikilink_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            from mimir_v8.reporting import DeepReader
            reader = DeepReader(store)
            reader.learn_dir = Path(tmp) / "learn"
            reader.learn_dir.mkdir(parents=True, exist_ok=True)
            content = "部署策略：新硬件 n100 应优先用于高频查询，不影响现有架构。必须遵守既有规则。"
            path = reader._write_reading_log(
                content,
                type("E", (), {"summary": "部署策略确认", "salience": 0.8,
                                "risk": "low", "domain": "infrastructure",
                                "fact_type": "pattern", "reasoning": "逻辑链"})(),
                ["部署策略确认"],
                title="N100 部署",
                source="manual",
            )
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("## 相关事实", text)
            self.assertIn("## Backlinks", text)


class TestM4MultiModal(unittest.TestCase):
    """M4: multi-modal asset references attach to facts with policies + audit."""

    def _seed(self, tmp, *, visibility="all", egress="redacted_external"):
        from mimir_v8.multimodal import MultiModalService
        store = CanonicalStore(Path(tmp) / "canonical.db")
        fid = store.create_fact(
            CreateFact(
                content="N100 server handles all request traffic at high throughput",
                summary="N100 server capacity", owner_principal="mentor",
                domain="infrastructure", fact_type="pattern",
                visibility=visibility, sensitivity="internal",
                egress_policy=egress, human_status="confirmed",
            ),
            actor_principal="mentor",
        )["fact_id"]
        return store, MultiModalService(store), fid

    def test_attach_creates_asset_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, fid = self._seed(tmp)
            result = svc.attach(fid, "image", "assets/n100-dash.png",
                                actor_principal="mentor")
            self.assertEqual(result["status"], "ok")
            assets = svc.list(fid)
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]["asset_kind"], "image")
            self.assertEqual(assets[0]["asset_ref"], "assets/n100-dash.png")

    def test_attach_writes_audit_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, fid = self._seed(tmp)
            svc.attach(fid, "image", "assets/n100.png")
            with store.connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM memory_events WHERE event_type='fact.asset_attached'"
                ).fetchone()[0]
            self.assertGreaterEqual(n, 1)

    def test_attach_rejects_unsupported_kind(self):
        import mimir_v8.multimodal as mod
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, fid = self._seed(tmp)
            with self.assertRaisesRegex(mod.AssetError, "unsupported asset_kind"):
                svc.attach(fid, "hologram", "x.obj")

    def test_attach_rejects_non_publishable_fact(self):
        import mimir_v8.multimodal as mod
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, fid = self._seed(tmp, visibility="owner_only")
            with self.assertRaisesRegex(mod.AssetError, "owner_only"):
                svc.attach(fid, "image", "assets/private.png")

    def test_attach_rejects_local_only_fact(self):
        import mimir_v8.multimodal as mod
        with tempfile.TemporaryDirectory() as tmp:
            store, svc, fid = self._seed(tmp, egress="local_only")
            with self.assertRaisesRegex(mod.AssetError, "local_only"):
                svc.attach(fid, "image", "assets/local.png")

    def test_embed_note_renders_obsidian_embed(self):
        from mimir_v8.wikilink import fact_note
        note = fact_note(
            {"fact_id": "f1", "content_hash": "aaaa", "content": "N100 server",
             "summary": "N100 primary", "domain": "infrastructure",
             "fact_type": "pattern", "confidence_score": 0.9},
            related=[],
            assets=[
                {"asset_kind": "image", "asset_ref": "assets/dash.png"},
                {"asset_kind": "document", "asset_ref": "docs/runbook.md"},
            ],
        )
        self.assertIn("![[assets/dash.png]]", note)
        self.assertIn("[[docs/runbook.md]]", note)
        self.assertIn("## 附件", note)

    def test_asset_tables_created_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with store.connect() as conn:
                present = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertIn("fact_assets", present)


class TestM1dHermesPluginContract(unittest.TestCase):
    """M1d: Hermes MemoryProvider plugin package imports and mirrors MEMORY.md."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path.home() / ".hermes/plugins/memory"))
        import importlib
        cls.pkg = importlib.import_module("mimir_memory_provider")
        cls.provider = importlib.import_module("mimir_memory_provider.provider")
        cls.tools = importlib.import_module("mimir_memory_provider.tools")

    def test_provider_hooks_exist(self):
        for name in ("on_turn_start", "on_turn_end", "before_context_compress", "on_memory_update"):
            self.assertTrue(callable(getattr(self.provider, name, None)), name)

    def test_tools_exist(self):
        for name in ("mimir_search", "mimir_remember", "mimir_recent", "mimir_reflect"):
            self.assertTrue(callable(getattr(self.tools, name, None)), name)

    def test_on_memory_update_writes_memory_md(self):
        import os
        from pathlib import Path
        target = Path(tempfile.mkdtemp()) / "MEMORY.md"
        saved = os.environ.get("MIMIR_PLUGIN_MEMORY_MD")
        try:
            os.environ["MIMIR_PLUGIN_MEMORY_MD"] = str(target)
            import importlib
            reloaded = importlib.reload(self.provider)
            result = reloaded.on_memory_update(snapshot=["alpha", "beta"])
            self.assertEqual(result["status"], "ok")
            self.assertTrue(target.is_file())
        finally:
            if saved is None:
                os.environ.pop("MIMIR_PLUGIN_MEMORY_MD", None)
            else:
                os.environ["MIMIR_PLUGIN_MEMORY_MD"] = saved


if __name__ == "__main__":
    unittest.main()