# -*- coding: utf-8 -*-
"""Issue #44: explicit, writer-declared supersession.

The bug this closes: a correction is written as a *new* fact (the ADD-only
gate forbids rewriting the old one), and retrieval then returns both the
wrong conclusion and its correction, with no way for a stateless agent to
tell which is current. The conflict detector cannot help — it matches on
Jaccard >= 0.6, and a correction's wording deliberately does not overlap
the text it corrects (measured 0.06 on the reported case).

So: the writer declares the replacement at write time. No background
inference, no automatic UPDATE — only an explicit request flips a status
enum, and `restore` puts it back.
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
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact, ValidationError
from mimir_v8.store import CanonicalStore, ConflictError, NotFoundError


class SupersedeFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        self.knowledge = KnowledgeService(self.store)
        self.unified = UnifiedSearch(self.query, self.knowledge)
        self.feedback = FeedbackLoop(self.store, self.knowledge)
        self.tokens = {
            "mentor": "sup-mentor-token",
            "other": "sup-other-token",
            "admin": "sup-admin-token",
        }
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": principal,
                        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "scopes": ["read", "write", "delete", "manage"],
                        "roles": ["admin"] if principal == "admin" else [],
                        "admin": principal == "admin",
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

    def make_fact(self, owner="mentor", key="sup", domain="infrastructure",
                  content="c", visibility="all"):
        return self.store.create_fact(
            CreateFact(content=content, owner_principal=owner, domain=domain,
                       fact_type="pattern", summary=content[:60],
                       visibility=visibility, idempotency_key=key),
            actor_principal="admin",
        )


class TestSupersedeStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fx = SupersedeFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_supersede_flips_status_and_links_the_pair(self):
        old = self.fx.make_fact(key="old", content="zen models truncate tool names")
        new = self.fx.make_fact(key="new", content="zen models are fine, gateway rewrites tools")

        result = self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]],
            relation="supersede", reason="earlier conclusion was a misdiagnosis",
            actor_principal="admin",
        )
        self.assertEqual(result["relation"], "supersede")
        self.assertEqual(len(result["superseded"]), 1)

        row = self.fx.store.get_fact(old["fact_id"])
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(row["current_version"], 2)
        # content is untouched — supersede is a status flip, not an edit
        self.assertEqual(row["content"], "zen models truncate tool names")

        self.assertEqual(
            [r["newer_fact_id"] for r in self.fx.store.superseded_by(old["fact_id"])],
            [new["fact_id"]],
        )
        # the new fact itself stays active
        self.assertEqual(self.fx.store.get_fact(new["fact_id"])["status"], "active")

    def test_amend_links_but_leaves_the_older_fact_active(self):
        old = self.fx.make_fact(key="old", content="deploy takes ten minutes")
        new = self.fx.make_fact(key="new", content="deploy takes ten minutes plus cache warm")

        self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]],
            relation="amend", actor_principal="admin",
        )
        row = self.fx.store.get_fact(old["fact_id"])
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["current_version"], 1)
        self.assertEqual(len(self.fx.store.superseded_by(old["fact_id"])), 1)

    def test_retract_marks_the_older_fact_retracted(self):
        old = self.fx.make_fact(key="old", content="the earth is flat")
        new = self.fx.make_fact(key="new", content="withdrawn claim")
        self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]],
            relation="retract", actor_principal="admin",
        )
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "retracted")

    def test_cross_domain_supersession_is_refused(self):
        old = self.fx.make_fact(key="old", domain="system", content="a")
        new = self.fx.make_fact(key="new", domain="infrastructure", content="b")
        with self.assertRaises(ValidationError):
            self.fx.store.supersede_fact(
                new["fact_id"], older_fact_ids=[old["fact_id"]],
                actor_principal="admin",
            )
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "active")

    def test_unknown_older_fact_fails_closed(self):
        new = self.fx.make_fact(key="new", content="b")
        with self.assertRaises(NotFoundError):
            self.fx.store.supersede_fact(
                new["fact_id"], older_fact_ids=["does-not-exist"],
                actor_principal="admin",
            )

    def test_unknown_relation_is_refused(self):
        old = self.fx.make_fact(key="old", content="a")
        new = self.fx.make_fact(key="new", content="b")
        with self.assertRaises(ValidationError):
            self.fx.store.supersede_fact(
                new["fact_id"], older_fact_ids=[old["fact_id"]],
                relation="delete-forever", actor_principal="admin",
            )

    def test_superseding_twice_is_a_conflict_not_a_silent_overwrite(self):
        old = self.fx.make_fact(key="old", content="a")
        first = self.fx.make_fact(key="first", content="b")
        second = self.fx.make_fact(key="second", content="c")
        self.fx.store.supersede_fact(
            first["fact_id"], older_fact_ids=[old["fact_id"]], actor_principal="admin")
        with self.assertRaises(ConflictError):
            self.fx.store.supersede_fact(
                second["fact_id"], older_fact_ids=[old["fact_id"]],
                actor_principal="admin")
        # the losing attempt leaves no link behind
        self.assertEqual(
            [r["newer_fact_id"] for r in self.fx.store.superseded_by(old["fact_id"])],
            [first["fact_id"]],
        )

    def test_restore_brings_a_superseded_fact_back(self):
        old = self.fx.make_fact(key="old", content="a")
        new = self.fx.make_fact(key="new", content="b")
        self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]], actor_principal="admin")
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "superseded")

        result = self.fx.store.restore_fact(
            old["fact_id"], actor_principal="admin", reason="changed my mind")
        self.assertTrue(result["restored"])
        self.assertEqual(result["previous_status"], "superseded")
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "active")
        # the audit trail of who superseded it survives the rollback
        self.assertEqual(len(self.fx.store.superseded_by(old["fact_id"])), 1)

    def test_supersede_writes_an_event_and_fans_out_to_projectors(self):
        old = self.fx.make_fact(key="old", content="a")
        new = self.fx.make_fact(key="new", content="b")
        self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]], actor_principal="admin")

        import contextlib
        with contextlib.closing(self.fx.store.connect()) as c:
            events = c.execute(
                "SELECT event_type, aggregate_id, aggregate_version FROM memory_events "
                "WHERE aggregate_id=? ORDER BY event_seq", (old["fact_id"],)
            ).fetchall()
            outbox = c.execute(
                "SELECT projector_name FROM outbox WHERE event_seq=("
                "SELECT MAX(event_seq) FROM memory_events WHERE aggregate_id=?)",
                (old["fact_id"],),
            ).fetchall()
        self.assertEqual([e["event_type"] for e in events], ["fact.created", "fact.superseded"])
        self.assertEqual(events[1]["aggregate_version"], 2)
        self.assertEqual(
            sorted(r["projector_name"] for r in outbox),
            ["core_memory", "fts", "graph", "vector"],
        )


class TestSupersedeRetrieval(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fx = SupersedeFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _search(self, **overrides):
        request = QueryRequest(
            text="zen models tool names",
            principal_id="mentor",
            limit=10,
            use_vector=False,
            use_graph=False,
            use_anchor=False,
            **overrides,
        )
        return self.fx.query.search(request)

    def test_default_search_drops_the_superseded_record(self):
        old = self.fx.make_fact(
            key="old", content="zen models truncate tool names and are unusable as agents")
        new = self.fx.make_fact(
            key="new", content="zen models are fine; the gateway rewrites tools")
        self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]], actor_principal="admin")

        hits = [r["fact_id"] for r in self._search()["results"]]
        self.assertIn(new["fact_id"], hits)
        self.assertNotIn(old["fact_id"], hits)

    def test_include_superseded_brings_it_back_with_the_edge(self):
        old = self.fx.make_fact(
            key="old", content="zen models truncate tool names and are unusable as agents")
        new = self.fx.make_fact(
            key="new", content="zen models are fine; the gateway rewrites tools")
        self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]], actor_principal="admin")

        payload = self._search(include_superseded=True)
        rows = {r["fact_id"]: r for r in payload["results"]}
        self.assertIn(old["fact_id"], rows)
        self.assertEqual(rows[old["fact_id"]]["status"], "superseded")
        self.assertEqual(rows[old["fact_id"]]["superseded_by"], [new["fact_id"]])


class TestSupersedeApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fx = SupersedeFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _create(self, content, key, supersedes=None, relation="supersede",
                domain="infrastructure", owner="mentor"):
        body = {
            "content": content,
            "owner_principal": owner,
            "domain": domain,
            "fact_type": "pattern",
            "summary": content[:60],
            "idempotency_key": key,
        }
        if supersedes is not None:
            body["supersedes"] = supersedes
            body["relation"] = relation
            body["supersede_reason"] = "correction"
        return self.fx.client.post(
            "/v8/facts", json=body, headers=self.fx.headers())

    def test_create_with_supersedes_marks_the_old_fact(self):
        old = self.fx.make_fact(key="old", content="zen is broken")
        response = self._create("zen is fine", "new", supersedes=[old["fact_id"]])
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["supersession"]["relation"], "supersede")
        self.assertEqual(
            self.fx.store.get_fact(old["fact_id"])["status"], "superseded")

    def test_a_failed_supersession_does_not_leave_the_new_fact_behind(self):
        # acceptance #6: one bad id in the list must roll back everything,
        # including the fact that would otherwise have been created.
        before = self.fx.store.counts()["facts"]
        response = self._create("zen is fine", "new", supersedes=["does-not-exist"])
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.fx.store.counts()["facts"], before)

    def test_cross_domain_through_the_api_is_rejected(self):
        # The issue text asks for 400; this codebase maps every ValidationError
        # to 422 through one handler, and changing that would move a
        # long-standing contract for unrelated routes. 422 it is — the
        # important half is that it is refused and nothing is written.
        old = self.fx.make_fact(key="old", domain="system", content="a")
        response = self._create("b", "new", domain="infrastructure",
                                supersedes=[old["fact_id"]])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "active")

    def test_superseding_another_principals_fact_is_403(self):
        theirs = self.fx.make_fact(owner="jarvis", key="theirs", content="a")
        response = self._create("b", "new", supersedes=[theirs["fact_id"]])
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.fx.store.get_fact(theirs["fact_id"])["status"], "active")

    def test_restore_brings_it_back_through_the_api(self):
        old = self.fx.make_fact(key="old", content="a")
        self._create("b", "new", supersedes=[old["fact_id"]])
        response = self.fx.client.post(
            f"/v8/facts/{old['fact_id']}/restore",
            json={"reason": "undo"},
            headers=self.fx.headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["restored"])
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "active")

    def test_query_endpoint_hides_superseded_by_default(self):
        old = self.fx.make_fact(key="old", content="zen models truncate tool names")
        self._create("zen models are fine", "new", supersedes=[old["fact_id"]])

        body = {"text": "zen models tool names", "limit": 10,
                "use_vector": False, "use_graph": False, "use_anchor": False}
        response = self.fx.client.post(
            "/v8/query", json=body, headers=self.fx.headers())
        self.assertEqual(response.status_code, 200, response.text)
        ids = [r["fact_id"] for r in response.json()["results"]]
        self.assertNotIn(old["fact_id"], ids)

        response = self.fx.client.post(
            "/v8/query", json={**body, "include_superseded": True},
            headers=self.fx.headers())
        self.assertEqual(response.status_code, 200, response.text)
        rows = {r["fact_id"]: r for r in response.json()["results"]}
        self.assertIn(old["fact_id"], rows)
        self.assertEqual(rows[old["fact_id"]]["status"], "superseded")


class TestSupersedeMigration(unittest.TestCase):
    def test_existing_database_can_be_widened_in_place(self):
        """issue #44 changes a CHECK constraint, which SQLite cannot ALTER.

        The migration rebuilds `facts` (12-step recipe). Prove the rebuild
        keeps every row, every child row, and leaves no dangling FK.
        """
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "legacy.db"
            store = CanonicalStore(db)

            # Build the pre-#44 shape by dropping the widened CHECK.
            import contextlib
            with contextlib.closing(store.connect()) as c:
                c.execute("PRAGMA foreign_keys=OFF")
                c.execute("""CREATE TABLE facts_pre44 (
                    fact_id TEXT PRIMARY KEY,
                    legacy_id TEXT UNIQUE,
                    current_version INTEGER NOT NULL CHECK (current_version >= 1),
                    status TEXT NOT NULL CHECK (status IN ('active','tombstoned','disputed','archived')),
                    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
                    summary TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    owner_principal TEXT NOT NULL,
                    project_id TEXT,
                    visibility TEXT NOT NULL CHECK (visibility IN ('all','shared','owner_only')),
                    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('internal','confidential','restricted')),
                    egress_policy TEXT NOT NULL CHECK (egress_policy IN ('local_only','redacted_external','external_allowed')),
                    human_status TEXT NOT NULL CHECK (human_status IN ('unreviewed','confirmed','rejected','disputed')),
                    confidence_score REAL,
                    valid_from TEXT,
                    valid_to TEXT,
                    recorded_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_verified_at TEXT,
                    tombstoned_at TEXT,
                    decay_tier TEXT NOT NULL DEFAULT 'L0_never',
                    decayed_at TEXT,
                    schema_version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL
                ) STRICT""")
                cols = ",".join(r[1] for r in c.execute("PRAGMA table_info(facts)"))
                c.execute(f"INSERT INTO facts_pre44 ({cols}) SELECT {cols} FROM facts")
                c.execute("DROP TABLE facts")
                c.execute("ALTER TABLE facts_pre44 RENAME TO facts")
                c.execute("DROP TABLE IF EXISTS fact_supersessions")
                c.commit()

            fact = store.create_fact(
                CreateFact(content="legacy row", owner_principal="mentor",
                           domain="system", fact_type="event", summary="s",
                           idempotency_key="legacy-1"),
                actor_principal="admin",
            )
            before = store.counts()

            store.migrate_schema_22_to_23()

            self.assertEqual(store.counts()["facts"], before["facts"])
            self.assertEqual(
                store.counts()["fact_versions"], before["fact_versions"])
            self.assertEqual(
                store.counts()["memory_events"], before["memory_events"])
            with contextlib.closing(store.connect()) as c:
                self.assertEqual(
                    c.execute("PRAGMA foreign_key_check").fetchall(), [])
            # the widened CHECK now accepts the new statuses
            self.assertEqual(store.get_fact(fact["fact_id"])["status"], "active")
            newer = store.create_fact(
                CreateFact(content="after migration", owner_principal="mentor",
                           domain="system", fact_type="event", summary="s2",
                           idempotency_key="legacy-2"),
                actor_principal="admin",
            )
            store.supersede_fact(
                newer["fact_id"], older_fact_ids=[fact["fact_id"]],
                actor_principal="admin")
            self.assertEqual(
                store.get_fact(fact["fact_id"])["status"], "superseded")

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            store.migrate_schema_22_to_23()
            store.migrate_schema_22_to_23()  # must not raise


if __name__ == "__main__":
    unittest.main()