"""Tests for v1.6.0 Step 2: declarative probe & source registry engineering.

Covers:
- probe_model_endpoint returns ok/unreachable + latency, never blocks past timeout
- probe results persist into the entities table (model_probe type) and link to a fact
- debt-replay queue: enqueue / fetch-due / complete round-trip
"""
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import build_opener, Request

from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact


# ---------------------------------------------------------------------------
# Probe scheduler
# ---------------------------------------------------------------------------

class _LocalServer:
    """A real local HTTP server that delays before responding.

    Using a genuine socket (not a stub urllib handler) means urllib's own
    socket timeout fires naturally, so the timeout test exercises real
    behaviour rather than a fake.
    """

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self._server = None
        self._thread = None

    def __enter__(self):
        class Handler(BaseHTTPRequestHandler):
            delay = self.delay

            def do_GET(self):
                if Handler.delay:
                    time.sleep(Handler.delay)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):  # silence
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1/models"

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _probe_store():
    root = Path(tempfile.mkdtemp())
    store = CanonicalStore(root / "canon.db")
    store.migrate_schema_21_to_22()
    return store


def test_probe_model_endpoint_ok():
    """A reachable endpoint returns ok with a positive latency and no error."""
    with _LocalServer(delay=0) as url:
        store = _probe_store()
        result = store.probe_model_endpoint(url, timeout=5)

    assert result["status"] == "ok"
    assert result["latency_ms"] >= 0
    assert "error" not in result or result["error"] is None


def test_probe_model_endpoint_unreachable():
    """A connection error is captured as unreachable with an error string."""
    # No such host on the local network => urllib raises URLError.
    store = _probe_store()
    result = store.probe_model_endpoint(
        "http://unreachable.invalid.local/v1/models", timeout=2
    )

    assert result["status"] == "unreachable"
    assert isinstance(result["error"], str) and result["error"]
    assert result["latency_ms"] >= 0


def test_probe_model_endpoint_respects_timeout():
    """A slow endpoint returns unreachable rather than blocking past timeout."""
    with _LocalServer(delay=2.0) as url:
        store = _probe_store()
        start = time.monotonic()
        result = store.probe_model_endpoint(url, timeout=0.3)
        elapsed = time.monotonic() - start

    # Must not block for the full 2s; returns within a small margin of the timeout.
    assert elapsed < 1.5
    assert result["status"] == "unreachable"
    assert "timeout" in (result.get("error") or "").lower()


# ---------------------------------------------------------------------------
# Probe result persistence
# ---------------------------------------------------------------------------

def test_record_probe_result_creates_entity_and_fact():
    """Recording a probe outcome creates a model_probe entity and links it to a fact."""
    store = _probe_store()
    fact = store.create_fact(
        CreateFact(
            content="Nemotron-3-Ultra is a free model in the pool.",
            summary="Nemotron-3-Ultra free model",
            domain="system",
            fact_type="project_config",
            owner_principal="heimdallr",
        ),
        actor_principal="heimdallr",
    )

    outcome = {
        "status": "ok",
        "latency_ms": 42.0,
        "error": None,
        "endpoint": "https://api.example.com/v1/models",
        "expected_status": [200],
    }
    entity_id = store.record_model_probe(
        model_name="Nemotron-3-Ultra",
        domain="free_model_pool",
        outcome=outcome,
        fact_id=fact["fact_id"],
        actor_principal="heimdallr",
    )

    entity = store.get_entity(entity_id)
    assert entity is not None
    assert entity["entity_type"] == "model_probe"
    assert entity["name"] == "Nemotron-3-Ultra"
    assert entity["status"] == "active"
    meta = entity["metadata_json"]
    assert '"status": "ok"' in meta
    assert '"latency_ms": 42' in meta

    linked = store.get_fact_entities(fact["fact_id"])
    assert any(e["entity_id"] == entity_id for e in linked)
    assert any(e["role"] == "probe_target" for e in linked)


def test_record_probe_result_unreachable_marks_suspended():
    """An unreachable probe marks the model_probe entity as suspended."""
    store = _probe_store()
    fact = store.create_fact(
        CreateFact(
            content="Llama-3.1-8B availability probe.",
            summary="Llama probe",
            domain="system",
            fact_type="project_config",
            owner_principal="heimdallr",
        ),
        actor_principal="heimdallr",
    )
    outcome = {
        "status": "unreachable",
        "latency_ms": 0.0,
        "error": "timed out",
        "endpoint": "https://api.example.com/v1/models/llama",
        "expected_status": [200],
    }
    entity_id = store.record_model_probe(
        model_name="Llama-3.1-8B",
        domain="free_model_pool",
        outcome=outcome,
        fact_id=fact["fact_id"],
        actor_principal="heimdallr",
    )
    # Suspended entities are excluded by default; callers checking outage state
    # must opt in via include_suspended=True.
    assert store.get_entity(entity_id) is None
    entity = store.get_entity(entity_id, include_suspended=True)
    assert entity["status"] == "suspended"


# ---------------------------------------------------------------------------
# Debt-replay queue
# ---------------------------------------------------------------------------

def test_queue_and_complete_probe_task_roundtrip():
    """A queued probe task can be fetched as due and then completed."""
    store = _probe_store()
    task_id = store.queue_probe_task(
        entity_id="ent_abc",
        endpoint="https://api.example.com/v1/models",
        snapshot={"status": "unreachable", "error": "timed out"},
        due_at="2026-01-01T00:00:00Z",
    )
    assert task_id

    due = store.get_due_probe_tasks(now="2099-01-01T00:00:00Z")
    assert any(t["task_id"] == task_id for t in due)

    ok = store.complete_probe_task(
        task_id,
        {"status": "ok", "latency_ms": 13.0, "error": None},
    )
    assert ok is True

    # After completion it is no longer due.
    due_after = store.get_due_probe_tasks(now="2099-01-01T00:00:00Z")
    assert not any(t["task_id"] == task_id for t in due_after)


def test_get_due_probe_tasks_respects_window():
    """Tasks with a future due_at are not returned for an earlier now."""
    store = _probe_store()
    store.queue_probe_task(
        entity_id="ent_future",
        endpoint="https://api.example.com/v1/models",
        snapshot={"status": "unreachable"},
        due_at="2099-01-01T00:00:00Z",
    )
    due = store.get_due_probe_tasks(now="2026-01-01T00:00:00Z")
    assert due == []
