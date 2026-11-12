# -*- coding: utf-8 -*-
"""Issue #35 layer 2: the timeline endpoint around an anchored fact.

Layer 1 (``detail=index``) lists facts and layer 3 (``/v8/query/expand``)
hydrates the selected ones. The missing middle layer answers "what was going
on when this memory changed", which neither of the other two can express.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.knowledge import FeedbackLoop, KnowledgeService, UnifiedSearch
from mimir_v8.query import QueryKernel
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class TimelineFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        self.knowledge = KnowledgeService(self.store)
        self.unified = UnifiedSearch(self.query, self.knowledge)
        self.feedback = FeedbackLoop(self.store, self.knowledge)
        self.tokens = {"mentor": "tl-mentor-token", "jarvis": "tl-jarvis-token"}
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": principal,
                        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "scopes": ["read", "write", "delete"],
                        "roles": [],
                        "admin": False,
                    }
                    for principal, token in self.tokens.items()
                ]
            }),
            encoding="utf-8",
        )
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(token_path),
            query=self.query,
            knowledge=self.knowledge,
            unified_search=self.unified,
            feedback_loop=self.feedback,
        )
        self.client = TestClient(create_app(context), raise_server_exceptions=False)

    def headers(self, principal="mentor"):
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    def make_fact(self, owner="mentor", key="tl", visibility="all"):
        return self.store.create_fact(
            CreateFact(content="c", owner_principal=owner, domain="system",
                       fact_type="event", summary="s", visibility=visibility,
                       idempotency_key=key),
            actor_principal="admin",
        )


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fixture = TimelineFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_timeline_anchors_on_the_fact_and_lists_neighbours(self):
        first = self.fixture.make_fact(key="tl-1")
        second = self.fixture.make_fact(key="tl-2")
        third = self.fixture.make_fact(key="tl-3")

        response = self.fixture.client.get(
            f"/v8/facts/{second['fact_id']}/timeline",
            params={"before": 1, "after": 1},
            headers=self.fixture.headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["anchor_event_seq"], second["event_seq"])
        seqs = [event["event_seq"] for event in payload["events"]]
        self.assertEqual(seqs, [first["event_seq"], second["event_seq"], third["event_seq"]])
        anchors = [event for event in payload["events"] if event["is_anchor"]]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["aggregate_id"], second["fact_id"])

    def test_unknown_fact_is_404(self):
        response = self.fixture.client.get(
            "/v8/facts/does-not-exist/timeline",
            headers=self.fixture.headers(),
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_neighbours_owned_by_others_are_not_visible(self):
        # visibility='owner_only' is what makes "mine" unreadable to jarvis:
        # the default 'all' grants role 'authenticated' a read, so any
        # authenticated caller sees it (and the filter is right to keep it).
        mine = self.fixture.make_fact(key="tl-mine", visibility="owner_only")
        theirs = self.fixture.make_fact(
            owner="jarvis", key="tl-theirs", visibility="owner_only")

        response = self.fixture.client.get(
            f"/v8/facts/{theirs['fact_id']}/timeline",
            params={"before": 5, "after": 5},
            headers=self.fixture.headers("jarvis"),
        )
        self.assertEqual(response.status_code, 200, response.text)
        their_ids = {event["aggregate_id"] for event in response.json()["events"]}
        # the anchor is always present, whatever the filters do
        self.assertIn(theirs["fact_id"], their_ids)
        # mentor's owner_only fact is not readable by jarvis: it must not
        # leak into the neighbourhood of a jarvis fact.
        self.assertNotIn(mine["fact_id"], their_ids)

        # ... and the boundary holds in the other direction too: a jarvis
        # fact does not surface in mentor's timeline.
        mentor_view = self.fixture.client.get(
            f"/v8/facts/{mine['fact_id']}/timeline",
            params={"before": 5, "after": 5},
            headers=self.fixture.headers("mentor"),
        )
        self.assertEqual(mentor_view.status_code, 200, mentor_view.text)
        mentor_ids = {event["aggregate_id"] for event in mentor_view.json()["events"]}
        self.assertIn(mine["fact_id"], mentor_ids)
        self.assertNotIn(theirs["fact_id"], mentor_ids)

    def test_reading_another_principals_fact_is_403(self):
        theirs = self.fixture.make_fact(owner="jarvis", key="tl-theirs2")
        response = self.fixture.client.get(
            f"/v8/facts/{theirs['fact_id']}/timeline",
            headers=self.fixture.headers("mentor"),
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_requested_window_is_clamped_in_both_directions(self):
        self.fixture.make_fact(key="tl-c1")
        fact_id = self.fixture.make_fact(key="tl-c2")["fact_id"]
        for bad in (-100, 500):
            with self.subTest(bad=bad):
                response = self.fixture.client.get(
                    f"/v8/facts/{fact_id}/timeline",
                    params={"before": bad, "after": bad},
                    headers=self.fixture.headers(),
                )
                # the route declares Query(ge=0, le=50), so an out-of-range
                # window is a 422 request-validation error rather than a
                # silently clamped 200.
                self.assertEqual(response.status_code, 422, response.text)

        # a legal maximum window still behaves: at most 2*50+1 events
        response = self.fixture.client.get(
            f"/v8/facts/{fact_id}/timeline",
            params={"before": 50, "after": 50},
            headers=self.fixture.headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["events"]
        seqs = [event["event_seq"] for event in events]
        self.assertLessEqual(len(events), 101)
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(
            [event["event_seq"] for event in events if event["is_anchor"]],
            [self.fixture.store.fact_timeline(fact_id)["anchor_event_seq"]],
        )


if __name__ == "__main__":
    unittest.main()
