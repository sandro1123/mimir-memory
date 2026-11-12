# -*- coding: utf-8 -*-
"""Issue #37 / #38: dead-letter recovery and failure detail.

A projector that hits the same failing event five times marks the row
``dead_letter`` and stops advancing its checkpoint, so semantic recall degrades
silently. Before this change the only recovery was editing the database by
hand, and the only record of *why* it died was the exception class name.
"""
import contextlib
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mimir_v8.operations import (
    OperationsError,
    requeue_dead_letters,
    reset_projector_stream,
)
from mimir_v8.projector import DEAD_LETTER_DETAIL_LIMIT, ProjectorRunner
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class _Boom:
    """Projector whose apply() always raises, to drive the retry ladder.

    Uses the real ``fts`` name because ``projector_state`` only has rows for
    the registered projectors.
    """

    name = "fts"

    def __init__(self, message="chroma write lock timeout"):
        self.message = message
        self.calls = 0

    def apply(self, event, fact):
        self.calls += 1
        raise RuntimeError(self.message)


def _seed(store, key="k1"):
    out = store.create_fact(
        CreateFact(content="c", owner_principal="mentor", domain="system",
                   fact_type="pattern", summary="s", idempotency_key=key),
        actor_principal="admin",
    )
    return out["fact_id"]


def _dead_lettered(tmp, message="chroma write lock timeout"):
    store = CanonicalStore(Path(tmp) / "canonical.db")
    _seed(store)
    projector = _Boom(message)
    runner = ProjectorRunner(store, projector)
    for _ in range(5):
        runner.run_once(limit=10)
    return store, projector, runner


class TestFailureDetail(unittest.TestCase):
    def test_detail_is_persisted_and_surfaces_in_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            _seed(store)
            runner = ProjectorRunner(store, _Boom())
            with self.assertLogs("mimir_v8.projector", level="ERROR") as captured:
                for _ in range(5):
                    runner.run_once(limit=10)

            with contextlib.closing(store.connect()) as c:
                row = c.execute(
                    """SELECT status, last_error_code, last_error_detail FROM outbox
                    WHERE projector_name='fts'"""
                ).fetchone()
            self.assertEqual(row["status"], "dead_letter")
            self.assertEqual(row["last_error_code"], "RuntimeError")
            self.assertEqual(row["last_error_detail"], "chroma write lock timeout")

            status = runner.status()
            self.assertEqual(status["dead_letter"], 1)
            self.assertIsNotNone(status["oldest_dead_letter_at"])
            self.assertEqual(status["last_error_detail"], "chroma write lock timeout")
            self.assertTrue(
                any("dead-lettered" in line for line in captured.output),
                "dead-letter transition must emit an ERROR log",
            )

    def test_detail_is_redacted_and_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _ = _dead_lettered(tmp, "api_key=sk-4166fcd0 " + "x" * 900)
            with contextlib.closing(store.connect()) as c:
                detail = c.execute(
                    "SELECT last_error_detail FROM outbox WHERE projector_name='fts'"
                ).fetchone()["last_error_detail"]
            self.assertNotIn("sk-4166fcd0", detail)
            self.assertLessEqual(len(detail), DEAD_LETTER_DETAIL_LIMIT)

    def test_successful_projection_clears_stale_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, projector, runner = _dead_lettered(tmp)
            requeue_dead_letters(store, "fts")
            projector.apply = lambda event, fact: None
            runner.run_once(limit=10)
            with contextlib.closing(store.connect()) as c:
                row = c.execute(
                    """SELECT status, last_error_code, last_error_detail FROM outbox
                    WHERE projector_name='fts'"""
                ).fetchone()
            self.assertEqual(row["status"], "done")
            self.assertIsNone(row["last_error_code"])
            self.assertIsNone(row["last_error_detail"])


class TestRecovery(unittest.TestCase):
    def test_requeue_only_revives_dead_letters_without_rewinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, runner = _dead_lettered(tmp)
            before = runner.status()["checkpoint_event_seq"]

            report = requeue_dead_letters(store, "fts")
            self.assertEqual(report["dead_letters_requeued"], 1)

            with contextlib.closing(store.connect()) as c:
                row = c.execute(
                    """SELECT status, attempts, last_error_detail FROM outbox
                    WHERE projector_name='fts'"""
                ).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], 0)
            self.assertIsNone(row["last_error_detail"])
            self.assertEqual(runner.status()["checkpoint_event_seq"], before)

    def test_requeue_only_reports_zero_on_healthy_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            _seed(store)
            self.assertEqual(requeue_dead_letters(store, "fts")["dead_letters_requeued"], 0)

    def test_full_reset_rewinds_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, runner = _dead_lettered(tmp)
            with contextlib.closing(store.connect()) as c:
                c.execute(
                    "UPDATE projector_state SET checkpoint_event_seq=7 WHERE projector_name='fts'"
                )
                c.commit()
            report = reset_projector_stream(store, "fts")
            self.assertEqual(report["events_reset"], 1)
            self.assertEqual(runner.status()["checkpoint_event_seq"], 0)

    def test_unknown_projector_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")
            with self.assertRaises(OperationsError):
                requeue_dead_letters(store, "nope")


class TestWorkerCommand(unittest.TestCase):
    def test_cli_requeues(self):
        from mimir_v8 import worker

        with tempfile.TemporaryDirectory() as tmp:
            _dead_lettered(tmp)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = worker.main([
                    "projector-reset", "--name", "fts",
                    "--data-dir", tmp, "--requeue-only",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["dead_letters_requeued"], 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
