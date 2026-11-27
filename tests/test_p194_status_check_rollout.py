# -*- coding: utf-8 -*-
"""1.9.4: roll the issue #44 status widening out to existing databases.

`migrate_schema_22_to_23` (the 12-step rebuild that widens the frozen
`facts.status` CHECK) has existed since 1.7.0, but nothing called it: the
migrate CLI had no route to it and the runtime never checks. Production
therefore kept the pre-#44 CHECK, and every supersession died inside the
status flip with an IntegrityError that surfaced as a bare 500.

These tests pin the three parts of the rollout:
1. a CLI route that takes a mandatory verified backup, then widens;
2. an un-widened database refuses supersession with a clear 503
   ``migration_required`` instead of a 500, and leaves nothing behind;
3. ``/health`` reports whether the widening is in place.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mimir_v8.store import CanonicalStore, SchemaVersionError

from tests.test_p44_supersede import SupersedeFixture

_PRE44_STATUS = "('active','tombstoned','disputed','archived')"


def make_pre44(store: CanonicalStore) -> None:
    """Rewind a fresh database to the pre-#44 `facts` shape.

    Takes the real DDL and narrows only the status CHECK, so the fixture
    cannot drift from the runtime table the way a hand-copied DDL would.
    """
    with contextlib.closing(store.connect()) as c:
        ddl = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='facts'"
        ).fetchone()[0]
        start = ddl.index("status TEXT NOT NULL CHECK")
        end = ddl.index(")),", start) + 2
        narrowed = (
            ddl[:start]
            + f"status TEXT NOT NULL CHECK (status IN {_PRE44_STATUS})"
            + ddl[end:]
        )
        narrowed = narrowed.replace("CREATE TABLE facts", "CREATE TABLE facts_pre44", 1)
        cols = ",".join(r[1] for r in c.execute("PRAGMA table_info(facts)"))
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute(narrowed)
        c.execute(f"INSERT INTO facts_pre44 ({cols}) SELECT {cols} FROM facts")
        c.execute("DROP TABLE facts")
        c.execute("ALTER TABLE facts_pre44 RENAME TO facts")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_owner_status "
            "ON facts(owner_principal, status)"
        )
        c.commit()


class TestFixture(unittest.TestCase):
    def test_make_pre44_really_narrows_the_check(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = CanonicalStore(Path(tmp) / "c.db")
            self.assertTrue(store.fact_status_widened())
            make_pre44(store)
            self.assertFalse(store.fact_status_widened())


class TestUnwidenedSupersession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fx = SupersedeFixture(Path(self._tmp.name))
        make_pre44(self.fx.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_store_refuses_with_schema_error_not_integrity_error(self):
        old = self.fx.make_fact(key="old", content="zen is broken")
        new = self.fx.make_fact(key="new", content="zen is fine")
        with self.assertRaises(SchemaVersionError) as caught:
            self.fx.store.supersede_fact(
                new["fact_id"], older_fact_ids=[old["fact_id"]],
                actor_principal="admin")
        self.assertIn("migrate", str(caught.exception))
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "active")

    def test_amend_still_works_because_it_flips_no_status(self):
        old = self.fx.make_fact(key="old", content="zen is broken")
        new = self.fx.make_fact(key="new", content="zen also has a quirk")
        result = self.fx.store.supersede_fact(
            new["fact_id"], older_fact_ids=[old["fact_id"]],
            relation="amend", actor_principal="admin")
        self.assertEqual(result["relation"], "amend")

    def test_api_answers_503_migration_required_and_leaves_no_new_fact(self):
        old = self.fx.make_fact(key="old", content="zen is broken")
        before = self.fx.store.counts()["facts"]
        response = self.fx.client.post(
            "/v8/facts",
            json={
                "content": "zen is fine",
                "owner_principal": "mentor",
                "domain": "infrastructure",
                "fact_type": "pattern",
                "summary": "zen is fine",
                "idempotency_key": "new-api",
                "supersedes": [old["fact_id"]],
                "relation": "supersede",
                "supersede_reason": "correction",
            },
            headers=self.fx.headers(),
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "migration_required")
        self.assertEqual(self.fx.store.counts()["facts"], before)
        self.assertEqual(self.fx.store.get_fact(old["fact_id"])["status"], "active")


class TestHealthReportsWidening(unittest.TestCase):
    def test_health_flag_follows_the_database(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            fx = SupersedeFixture(Path(tmp))
            body = fx.client.get("/health").json()
            self.assertIs(body["fact_status_widened"], True)
            make_pre44(fx.store)
            body = fx.client.get("/health").json()
            self.assertEqual(body["status"], "ok")
            self.assertIs(body["fact_status_widened"], False)


class TestWidenCli(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "mimir_v8.migrate_cli", *args],
            capture_output=True, text=True, timeout=120,
        )

    def test_cli_backs_up_then_widens_and_keeps_every_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            fx = SupersedeFixture(Path(tmp))
            old = fx.make_fact(key="old", content="zen is broken")
            make_pre44(fx.store)
            before = fx.store.counts()
            db = Path(tmp) / "canonical.db"
            backup = Path(tmp) / "backups" / "pre-widen.db"

            proc = self._run("widen-fact-status", "--database", str(db),
                             "--backup", str(backup))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertIs(report["widened_before"], False)
            self.assertIs(report["widened_after"], True)
            self.assertEqual(len(report["backup_sha256"]), 64)

            self.assertTrue(backup.is_file())
            with contextlib.closing(sqlite3.connect(backup)) as b:
                ddl = b.execute(
                    "SELECT sql FROM sqlite_master WHERE name='facts'").fetchone()[0]
            self.assertIn(_PRE44_STATUS, ddl, "backup must hold the pre-migration shape")

            store = CanonicalStore(db)
            self.assertTrue(store.fact_status_widened())
            for table in ("facts", "fact_versions", "memory_events"):
                self.assertEqual(store.counts()[table], before[table], table)
            new = fx.make_fact(key="new", content="zen is fine")
            store.supersede_fact(new["fact_id"], older_fact_ids=[old["fact_id"]],
                                 actor_principal="admin")
            self.assertEqual(store.get_fact(old["fact_id"])["status"], "superseded")

    def test_cli_refuses_an_existing_backup_and_touches_nothing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            make_pre44(store)
            backup = Path(tmp) / "taken.db"
            backup.write_bytes(b"not a database")
            proc = self._run("widen-fact-status", "--database",
                             str(Path(tmp) / "canonical.db"), "--backup", str(backup))
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(store.fact_status_widened())
            self.assertEqual(backup.read_bytes(), b"not a database")

    def test_cli_on_an_already_widened_database_is_a_no_op(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            CanonicalStore(Path(tmp) / "canonical.db")
            proc = self._run("widen-fact-status", "--database",
                             str(Path(tmp) / "canonical.db"),
                             "--backup", str(Path(tmp) / "b.db"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertIs(report["widened_before"], True)
            self.assertIs(report["widened_after"], True)


if __name__ == "__main__":
    unittest.main()
