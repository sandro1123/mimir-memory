import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from mimir_v8.learning import ConversationEnvelope, ConversationMessage, LearningService
from mimir_v8.retention import RetentionSchedule, RetentionService
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore, new_id


def past_due() -> str:
    return (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0).isoformat() + "Z"


def make_store(tmp: str) -> CanonicalStore:
    return CanonicalStore(Path(tmp) / "canonical.db")


def create_active_fact(store: CanonicalStore, content: str = "fact content") -> dict:
    created = store.create_fact(
        CreateFact(
            content=content,
            summary="s",
            owner_principal="mentor",
            domain="knowledge",
            fact_type="learning",
            sensitivity="internal",
            idempotency_key=f"fact:{new_id()}",
        ),
        actor_principal="tester",
    )
    return store.get_fact(created["fact_id"])


def ingest_message(store: CanonicalStore, content: str = "secret message"):
    learning = LearningService(store)
    env = ConversationEnvelope(
        connector_type="hermes_cdc",
        connector_id="test",
        session_id=None,
        owner_principal="mentor",
        memory_mode="observe",
        retention_class="standard",
        messages=(ConversationMessage(role="user", content=content),),
        source_uri="https://example.com",
        title="test",
        idempotency_key=f"msg:{new_id()}",
    )
    result = learning.ingest_conversation(env, "service:test")
    source_id = result["source_id"]
    with store.connect() as conn:
        row = conn.execute(
            "SELECT message_id, content_redacted FROM conversation_messages WHERE source_id=?",
            (source_id,),
        ).fetchone()
    return source_id, row["message_id"]


class TestRetentionFailClosed(unittest.TestCase):
    def test_fact_tombstone_failure_leaves_fact_and_job_unexecuted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            retention = RetentionService(store)
            fact = create_active_fact(store, content="must survive")
            sched = retention.schedule(
                RetentionSchedule(
                    resource_type="fact",
                    resource_id=fact["fact_id"],
                    due_at=past_due(),
                    reason="test",
                    idempotency_key=f"ret:{new_id()}",
                ),
                actor_principal="tester",
            )
            original = store.get_fact(fact["fact_id"])
            with mock.patch.object(store, "tombstone_fact", side_effect=RuntimeError("tombstone boom")):
                with self.assertRaises(RuntimeError):
                    retention.execute_due(actor_principal="tester")
            after = store.get_fact(fact["fact_id"])
            self.assertEqual(after["content"], "must survive")
            self.assertEqual(after["status"], original["status"])
            self.assertEqual(after["current_version"], original["current_version"])
            with store.connect() as conn:
                job = conn.execute(
                    "SELECT status FROM retention_jobs WHERE retention_job_id=?",
                    (sched["retention_job_id"],),
                ).fetchone()
            self.assertNotEqual(job["status"], "executed")

    def test_message_ledger_failure_leaves_content_and_job_unexecuted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            retention = RetentionService(store)
            source_id, message_id = ingest_message(store, content="secret message")
            sched = retention.schedule(
                RetentionSchedule(
                    resource_type="conversation_message",
                    resource_id=message_id,
                    due_at=past_due(),
                    reason="test",
                    idempotency_key=f"ret:{new_id()}",
                ),
                actor_principal="tester",
            )

            original_transaction = store.transaction

            @contextmanager
            def failing_ledger_transaction():
                with original_transaction() as conn:
                    class FailOnExecutedEvent:
                        def __init__(self, c):
                            self._c = c
                        def execute(self, sql, params=()):
                            if "INSERT INTO memory_events" in sql and params and len(params) > 5 and params[4] == "retention.executed":
                                raise RuntimeError("ledger boom")
                            return self._c.execute(sql, params)
                        def __getattr__(self, name):
                            return getattr(self._c, name)
                    yield FailOnExecutedEvent(conn)

            with mock.patch.object(store, "transaction", side_effect=failing_ledger_transaction):
                with self.assertRaises(RuntimeError):
                    retention.execute_due(actor_principal="tester")

            with store.connect() as conn:
                msg = conn.execute(
                    "SELECT content_redacted FROM conversation_messages WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                job = conn.execute(
                    "SELECT status FROM retention_jobs WHERE retention_job_id=?",
                    (sched["retention_job_id"],),
                ).fetchone()
            self.assertEqual(msg["content_redacted"], "secret message")
            self.assertNotEqual(job["status"], "executed")


if __name__ == "__main__":
    unittest.main()
