"""P1-6 防爆破 test: per-IP auth-failure limiter on the Mímir API.

Verifies:
  1. repeated invalid-token requests (>= max_failures) trip the limiter -> 429
  2. while locked, even a VALID token is rejected with 429
  3. unauthenticated 2xx (e.g. /health) does NOT reset the failure counter
  4. an authenticated success clears the counter before the lock trips
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mimir_v8.api import AuthFailureLimiter, ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.extraction import ExtractionService
from mimir_v8.query import QueryKernel
from mimir_v8.store import CanonicalStore, new_id


def _build_client(root: Path):
    store = CanonicalStore(root / "canonical.db")
    good_token = f"good-token-{new_id()}"
    token_path = root / f"tokens-{new_id()}.json"
    token_path.write_text(
        json.dumps({
            "principals": [{
                "id": "mentor",
                "token_sha256": hashlib.sha256(good_token.encode("utf-8")).hexdigest(),
                "scopes": ["read", "write", "ingest"],
                "roles": [],
                "admin": False,
            }]
        }),
        encoding="utf-8",
    )
    context = ServiceContext(
        store=store,
        token_store=TokenStore(token_path),
        query=QueryKernel(store),
        extraction=ExtractionService(store),
    )
    app = create_app(context)
    return TestClient(app, raise_server_exceptions=False), good_token


class AuthFailureLimiterUnitTest(unittest.TestCase):
    class _Req:
        class client:
            host = "10.0.0.9"

    def test_locks_at_max_failures_not_before(self) -> None:
        limiter = AuthFailureLimiter(max_failures=3, window_seconds=300.0, lock_seconds=300.0)
        req = self._Req()
        self.assertFalse(limiter.is_locked(req))
        limiter.record_failure(req)
        limiter.record_failure(req)
        self.assertFalse(limiter.is_locked(req), "below threshold must not lock")
        limiter.record_failure(req)
        self.assertTrue(limiter.is_locked(req), "at threshold must lock")

    def test_lock_expires_after_lock_seconds(self) -> None:
        limiter = AuthFailureLimiter(max_failures=2, window_seconds=300.0, lock_seconds=0.1)
        req = self._Req()
        limiter.record_failure(req)
        limiter.record_failure(req)
        self.assertTrue(limiter.is_locked(req))
        time.sleep(0.15)
        self.assertFalse(limiter.is_locked(req), "lock must expire after lock_seconds")

    def test_success_before_lock_clears_failure_counter(self) -> None:
        limiter = AuthFailureLimiter(max_failures=3, window_seconds=300.0, lock_seconds=300.0)
        req = self._Req()
        limiter.record_failure(req)
        limiter.record_failure(req)
        limiter.record_success(req)  # clears the two accumulated failures
        limiter.record_failure(req)
        limiter.record_failure(req)
        # only 2 failures since the success -> still below the threshold of 3
        self.assertFalse(limiter.is_locked(req), "success must reset the failure counter")


class BruteForceAPITest(unittest.TestCase):
    MAX_FAILURES = 10  # must match create_app wiring

    def test_brute_force_invalid_tokens_trips_429(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client, good_token = _build_client(Path(tmp))
            bad_headers = {"Authorization": "Bearer totally-wrong-token"}

            for i in range(self.MAX_FAILURES):
                resp = client.post("/v8/query", json={"text": "x"}, headers=bad_headers)
                self.assertEqual(401, resp.status_code, f"attempt {i}: {resp.text}")

            # next request from the same IP is rate-limited before auth
            resp = client.post("/v8/query", json={"text": "x"}, headers=bad_headers)
            self.assertEqual(429, resp.status_code, resp.text)
            self.assertEqual("rate_limited", resp.json()["error"]["code"])

    def test_valid_token_still_rejected_while_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client, good_token = _build_client(Path(tmp))
            bad_headers = {"Authorization": "Bearer totally-wrong-token"}
            for _ in range(self.MAX_FAILURES):
                client.post("/v8/query", json={"text": "x"}, headers=bad_headers)

            good_headers = {"Authorization": f"Bearer {good_token}"}
            resp = client.post("/v8/query", json={"text": "x"}, headers=good_headers)
            self.assertEqual(429, resp.status_code,
                             "valid token must be blocked while IP is locked")

    def test_unauthenticated_2xx_does_not_reset_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client, good_token = _build_client(Path(tmp))
            bad_headers = {"Authorization": "Bearer totally-wrong-token"}
            for _ in range(self.MAX_FAILURES):
                client.post("/v8/query", json={"text": "x"}, headers=bad_headers)

            # /health needs no auth and returns 200 — must NOT clear the lock
            resp = client.get("/health")
            self.assertEqual(200, resp.status_code)

            resp = client.post("/v8/query", json={"text": "x"}, headers=bad_headers)
            self.assertEqual(429, resp.status_code,
                             "unauthenticated 2xx must not reset the limiter")

    def test_authenticated_success_clears_failure_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client, good_token = _build_client(Path(tmp))
            bad_headers = {"Authorization": "Bearer totally-wrong-token"}
            good_headers = {"Authorization": f"Bearer {good_token}"}

            # accumulate failures below the threshold
            for _ in range(self.MAX_FAILURES - 1):
                client.post("/v8/query", json={"text": "x"}, headers=bad_headers)
            # a successful authenticated request clears the counter
            # (memories/recent is a plain DB read -> 200 on an empty store)
            resp = client.get("/v8/memories/recent", headers=good_headers)
            self.assertEqual(200, resp.status_code, resp.text)

            # now MAX_FAILURES-1 more failures must still not trip the lock
            for _ in range(self.MAX_FAILURES - 1):
                resp = client.post("/v8/query", json={"text": "x"}, headers=bad_headers)
                self.assertEqual(401, resp.status_code,
                                 "counter was not cleared by the successful auth")


if __name__ == "__main__":
    unittest.main()
