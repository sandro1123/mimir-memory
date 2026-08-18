"""Bearer principal authentication for the Mímir v8 service boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from pathlib import Path


class AuthError(RuntimeError):
    def __init__(self, message: str, status_code: int = 401, code: str = "authentication_failed"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class Principal:
    principal_id: str
    scopes: frozenset[str]
    roles: frozenset[str] = frozenset()
    is_admin: bool = False

    def require(self, scope: str) -> None:
        if self.is_admin or scope in self.scopes:
            return
        raise AuthError(f"missing scope: {scope}", 403, "missing_scope")

    def can_act_as(self, principal_id: str) -> bool:
        return self.is_admin or self.principal_id == principal_id


class TokenStore:
    """Hot-reload a hash-only token registry and fail closed on errors."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._mtime_ns: int | None = None
        self._records: tuple[tuple[str, Principal], ...] = ()

    def _load(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError as exc:
            raise AuthError("authentication is not configured", 503, "auth_unavailable") from exc
        if self._mtime_ns == stat.st_mtime_ns and self._records:
            return
        with self._lock:
            try:
                stat = self.path.stat()
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise AuthError("authentication registry is invalid", 503, "auth_unavailable") from exc
            records = []
            principal_ids = set()
            for item in data.get("principals", []):
                principal_id = str(item.get("id", "")).strip()
                token_hash = str(item.get("token_sha256", "")).strip().lower()
                if not principal_id or len(token_hash) != 64:
                    raise AuthError("authentication registry is invalid", 503, "auth_unavailable")
                if principal_id in principal_ids:
                    raise AuthError("authentication registry contains duplicate principals", 503, "auth_unavailable")
                try:
                    int(token_hash, 16)
                except ValueError as exc:
                    raise AuthError("authentication registry is invalid", 503, "auth_unavailable") from exc
                principal_ids.add(principal_id)
                records.append(
                    (
                        token_hash,
                        Principal(
                            principal_id=principal_id,
                            scopes=frozenset(str(value) for value in item.get("scopes", [])),
                            roles=frozenset(str(value) for value in item.get("roles", [])),
                            is_admin=bool(item.get("admin", False)),
                        ),
                    )
                )
            if not records:
                raise AuthError("authentication has no principals", 503, "auth_unavailable")
            self._records = tuple(records)
            self._mtime_ns = stat.st_mtime_ns

    def validate(self) -> int:
        self._load()
        return len(self._records)

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("missing bearer token", 401, "missing_token")
        token = authorization[7:].strip()
        if not token:
            raise AuthError("empty bearer token", 401, "missing_token")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self._load()
        matched = None
        for expected, principal in self._records:
            if hmac.compare_digest(digest, expected):
                matched = principal
        if matched is None:
            raise AuthError("invalid bearer token", 401, "invalid_token")
        return matched
