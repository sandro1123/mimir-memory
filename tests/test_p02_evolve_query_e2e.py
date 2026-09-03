"""P0-2 e2e guardrail: EvolveMem feedback -> confidence adjust -> queryable.

Verifies the full evolve->query传导 chain on an isolated temp store:
  1. create a fact
  2. submit >= MIN_SIGNALS_PER_FACT useful feedback signals
  3. run evolve()
  4. assert confidence moved up by CONFIDENCE_DELTA and a fact.evolved event landed
  5. assert the fact is still retrievable via QueryKernel
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mimir_v8.evolve import CONFIDENCE_DELTA, MIN_SIGNALS_PER_FACT, EvolveMemService
from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class EvolveQueryE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CanonicalStore(self.root / "canonical.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create_fact(self, content: str, summary: str) -> str:
        result = self.store.create_fact(
            CreateFact(
                content=content,
                owner_principal="mentor",
                domain="system",
                fact_type="event",
                summary=summary,
            ),
            actor_principal="mentor",
        )
        return result["fact_id"]

    def test_evolve_useful_signals_raise_confidence_and_fact_queryable(self) -> None:
        fact_id = self._create_fact(
            "P0-2 guardrail: evolve useful feedback must raise confidence.",
            "evolve e2e guardrail fact",
        )
        before = self.store.get_fact(fact_id)["confidence_score"]
        before = before if before is not None else 0.5

        evolve = EvolveMemService(self.store)
        # submit enough useful signals to clear MIN_SIGNALS_PER_FACT with ratio 1.0
        for i in range(MIN_SIGNALS_PER_FACT):
            evolve.submit_feedback(
                query_text="evolve e2e query",
                fact_id=fact_id,
                signal="useful",
                user_principal="mentor",
                actor_principal="service:evolve",
            )

        result = evolve.evolve(actor_principal="service:evolve")
        self.assertEqual(result["adjusted"], 1, f"expected 1 adjusted fact, got {result}")

        after = self.store.get_fact(fact_id)["confidence_score"]
        self.assertAlmostEqual(after, min(1.0, before + CONFIDENCE_DELTA), places=2)

        # a fact.evolved audit event must exist on the event ledger
        with self.store.connect() as conn:
            evolved = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_events WHERE aggregate_id=? AND event_type='fact.evolved'",
                (fact_id,),
            ).fetchone()["n"]
        self.assertGreaterEqual(evolved, 1, "fact.evolved event missing from ledger")

        # the fact must remain retrievable through the query kernel.
        # Project the fact into FTS first (create_fact enqueued outbox rows).
        fts = FTSProjector(self.root / "fts.db")
        ProjectorRunner(self.store, fts).run_once(limit=100)
        kernel = QueryKernel(self.store, fts=fts)
        response = kernel.search(QueryRequest(
            text="evolve e2e guardrail",
            principal_id="mentor",
            limit=10,
            use_vector=False,  # isolated store has no vector projection
            use_graph=False,
            depth="deep",  # v12.2 layered assembly: this fixture's fact_type
            # ("event") sits in LAYER1 — standard-depth search deliberately
            # skips L1 atoms, so drill deep to assert retrieval.
        ))
        hit_ids = {r["fact_id"] for r in response.get("results", [])}
        self.assertIn(fact_id, hit_ids, "evolved fact not retrievable via QueryKernel")

    def test_evolve_below_min_signals_does_not_adjust(self) -> None:
        fact_id = self._create_fact(
            "P0-2 guardrail: below MIN_SIGNALS must not adjust confidence.",
            "evolve below-min fact",
        )
        # Compare raw values symmetrically: a fresh fact may have
        # confidence_score=None, and evolve must leave it untouched.
        before = self.store.get_fact(fact_id)["confidence_score"]

        evolve = EvolveMemService(self.store)
        for i in range(MIN_SIGNALS_PER_FACT - 1):
            evolve.submit_feedback(
                query_text="q", fact_id=fact_id, signal="useful",
                user_principal="mentor", actor_principal="service:evolve",
            )
        result = evolve.evolve(actor_principal="service:evolve")
        self.assertEqual(result["adjusted"], 0)
        after = self.store.get_fact(fact_id)["confidence_score"]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
