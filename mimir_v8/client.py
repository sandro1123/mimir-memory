"""Single REST client contract shared by the v8 CLI and MCP adapter."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class APIClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, code: str = "client_error", detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ClientConfig:
    base_url: str = "http://127.0.0.1:8456"
    principal_id: str = "heimdallr"
    token: str = ""
    timeout: float = 30.0


def load_token(principal_id: str, token_file: str | Path | None = None) -> str:
    environment_token = os.environ.get("MIMIR_V8_TOKEN", "").strip()
    if environment_token:
        return environment_token
    configured = token_file or os.environ.get("MIMIR_V8_CLIENT_TOKEN_FILE")
    candidates = (
        [Path(configured).expanduser()]
        if configured
        else [
            Path.home() / ".hermes/mimir/secrets" / f"{principal_id}.token",
            Path.home() / ".hermes/mimir/secrets/clients" / f"{principal_id}.token",
        ]
    )
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    raise APIClientError(f"cannot load client token for {principal_id}", code="token_unavailable")


class MimirAPIClient:
    def __init__(self, config: ClientConfig | None = None):
        self.config = config or ClientConfig(
            base_url=os.environ.get("MIMIR_V8_URL", "http://127.0.0.1:8456"),
            principal_id=os.environ.get("MIMIR_AGENT", "heimdallr"),
            token=os.environ.get("MIMIR_V8_TOKEN", ""),
            timeout=float(os.environ.get("MIMIR_V8_TIMEOUT", "30")),
        )
        if not self.config.token:
            self.config = ClientConfig(
                base_url=self.config.base_url,
                principal_id=self.config.principal_id,
                token=load_token(self.config.principal_id),
                timeout=self.config.timeout,
            )
        self.base_url = self.config.base_url.rstrip("/")

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, authenticated: bool = True) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            query = urlencode({key: value for key, value in params.items() if value is not None})
            if query:
                url = f"{url}?{query}"
        headers = {
            "Accept": "application/json",
            "X-Request-ID": str(uuid.uuid4()),
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self.config.token}"
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(url, data=payload, headers=headers, method=method), timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8", errors="replace"))
            except (ValueError, json.JSONDecodeError):
                data = {"error": {"message": f"HTTP {exc.code}"}}
            error = data.get("error", {}) if isinstance(data, dict) else {}
            if isinstance(error, dict):
                raise APIClientError(
                    str(error.get("message", f"HTTP {exc.code}")),
                    status_code=exc.code,
                    code=str(error.get("code", "http_error")),
                    detail=error,
                ) from exc
            raise APIClientError(f"HTTP {exc.code}", status_code=exc.code, code="http_error") from exc
        except (URLError, TimeoutError) as exc:
            raise APIClientError("Mímir API is unavailable", code="service_unavailable") from exc
        except json.JSONDecodeError as exc:
            raise APIClientError("Mímir API returned invalid JSON", code="invalid_response") from exc

    def health(self) -> dict:
        return self.request("GET", "/v8/health", authenticated=False)

    def ready(self) -> dict:
        return self.request("GET", "/v8/ready", authenticated=False)

    def query(self, text: str, *, limit: int = 10, owner_principal=None,
              domain=None, fact_type=None, use_vector=True, use_fts=True, use_graph=True) -> dict:
        return self.request("POST", "/v8/query", body={
            "text": text, "limit": limit, "owner_principal": owner_principal,
            "domain": domain, "fact_type": fact_type, "use_vector": use_vector,
            "use_fts": use_fts, "use_graph": use_graph,
        })

    def create_fact(self, body: dict) -> dict:
        return self.request("POST", "/v8/facts", body=body)

    def awareness(self, agent_id: str | None = None, hours: int | None = None) -> dict:
        return self.request("GET", "/awareness", params={
            "agent": agent_id or self.config.principal_id, "hours": hours,
        })

    def stats(self) -> dict:
        return self.request("GET", "/stats")

    def core_memory_inject(self, agent_id: str, max_chars: int = 2000) -> dict:
        return self.request("GET", f"/v8/core-memory/{agent_id}/inject", params={"max_chars": max_chars})

    def ingest_conversation(self, body: dict) -> dict:
        return self.request("POST", "/v8/ingestion/conversations", body=body)

    def remember(self, body: dict) -> dict:
        return self.request("POST", "/v8/learning/remember", body=body)

    def forget(self, body: dict) -> dict:
        return self.request("POST", "/v8/learning/forget", body=body)

    def correct(self, body: dict) -> dict:
        return self.request("POST", "/v8/learning/correct", body=body)

    def list_candidates(self, *, status: str | None = None, limit: int = 50) -> dict:
        return self.request("GET", "/v8/learning/candidates", params={"status": status, "limit": limit})

    def review_candidate(self, candidate_id: str, body: dict) -> dict:
        return self.request("POST", f"/v8/learning/candidates/{candidate_id}/review", body=body)

    def commit_candidate(self, candidate_id: str, body: dict) -> dict:
        return self.request("POST", f"/v8/learning/candidates/{candidate_id}/commit", body=body)

    def submit_feedback(self, body: dict) -> dict:
        return self.request("POST", "/v8/learning/feedback", body=body)

    def learning_status(self) -> dict:
        return self.request("GET", "/v8/learning/status")

    # ── v12 Insight (M2/M3) ─────────────────────────────────────────────
    def search_trace(self, text: str, *, limit: int = 10,
                     dedup_threshold: float = 0.8, candidate_limit: int = 50) -> dict:
        """v12 recall funnel trace (five-stage query path)."""
        return self.request("POST", "/v12/search/trace",
                            params={"dedup_threshold": dedup_threshold},
                            body={"text": text, "limit": limit,
                                  "candidate_limit": candidate_limit})

    def evolve_feedback(self, query_text: str, fact_id: str, signal: str,
                        user_principal: str | None = None) -> dict:
        """v12 EvolveMem: submit a useful/useless/correction signal."""
        return self.request("POST", "/v12/evolve/feedback", body={
            "query_text": query_text, "fact_id": fact_id, "signal": signal,
            "user_principal": user_principal,
        })

    def evolve_report(self) -> dict:
        """v12 EvolveMem: 7-day retrieval quality report."""
        return self.request("GET", "/v12/evolve/report")

    def conflict_detect(self, threshold: float = 0.6) -> dict:
        """v12 conflict detection scan."""
        return self.request("POST", "/v12/conflicts/detect",
                            params={"threshold": threshold})

    def conflict_list(self, status: str = "open", limit: int = 50) -> dict:
        return self.request("GET", "/v12/conflicts",
                            params={"status": status, "limit": limit})

    def conflict_resolve(self, conflict_id: str, winner_fact_id: str,
                         reason: str = "") -> dict:
        return self.request("POST", f"/v12/conflicts/{conflict_id}/resolve",
                            body={"winner_fact_id": winner_fact_id, "reason": reason})

    def conflict_dismiss(self, conflict_id: str, reason: str = "") -> dict:
        return self.request("POST", f"/v12/conflicts/{conflict_id}/dismiss",
                            body={"reason": reason})

    def crystal_scan(self, window_days: int = 7, min_freq: int = 3) -> dict:
        return self.request("POST", "/v12/crystals/scan",
                            params={"window_days": window_days, "min_freq": min_freq})

    def crystal_list(self, status: str = "candidate", limit: int = 50) -> dict:
        return self.request("GET", "/v12/crystals",
                            params={"status": status, "limit": limit})

    def crystal_approve(self, candidate_id: str,
                        owner_principal: str | None = None) -> dict:
        return self.request("POST", f"/v12/crystals/{candidate_id}/approve",
                            body={"owner_principal": owner_principal})

    def crystal_dismiss(self, candidate_id: str, reason: str = "") -> dict:
        return self.request("POST", f"/v12/crystals/{candidate_id}/dismiss",
                            body={"reason": reason})

    def asset_attach(self, fact_id: str, asset_kind: str, asset_ref: str) -> dict:
        """v12 multi-modal: attach a media asset reference to a fact."""
        return self.request("POST", f"/v12/facts/{fact_id}/assets",
                            body={"asset_kind": asset_kind, "asset_ref": asset_ref})

    def asset_list(self, fact_id: str) -> dict:
        return self.request("GET", f"/v12/facts/{fact_id}/assets")
