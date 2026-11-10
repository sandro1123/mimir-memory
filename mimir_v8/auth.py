"""Bearer principal authentication for the Mímir v8 service boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
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
        if self.is_admin or self.principal_id == principal_id:
            return True
        aliases = PRINCIPAL_ALIASES.get(self.principal_id, frozenset())
        return principal_id in aliases


PRINCIPAL_ALIASES: dict[str, frozenset[str]] = {
    "heimdallr": frozenset({"mentor", "heimdallr-ex", "heimdallr"}),
}

#: Scopes the service actually checks (api.py ``scoped(...)``). Minting a
#: principal with a scope outside this set grants nothing and hides typos, so
#: it fails closed instead of being written into the registry.
KNOWN_SCOPES = frozenset({"read", "write", "delete", "ingest", "review", "manage", "admin"})

#: Client-chosen principal ids follow the same class as the X-Mimir-Agent
#: header, so a name learned from the header can be minted without renaming.
PRINCIPAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


def atomic_write_registry(path: str | Path, content: str) -> None:
    """P0-N（嘟嘟🟡3）：token registry 的原子写协议（运维侧唯一合法写法）。

    直接 `echo > tokens.json` 会让 TokenStore 的 mtime 触发式热加载在
    中间态读到半截 JSON → auth_unavailable 全站 503。协议=同目录临时
    文件写入 + fsync + os.replace（同文件系统原子改名，读侧要么看到
    完整旧表要么看到完整新表，无半截窗口）。
    """
    import os as _os
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp-atomic")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        _os.fsync(f.fileno())
    _os.replace(tmp, target)


def _file_key(stat) -> tuple[int, int, int]:
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _read_registry(path: str | Path) -> dict:
    """Read the token registry, failing closed on anything malformed."""
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuthError("authentication is not configured", 503, "auth_unavailable") from exc
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise AuthError("authentication registry is invalid", 503, "auth_unavailable") from exc
    if not isinstance(data, dict) or not isinstance(data.get("principals"), list):
        raise AuthError("authentication registry is invalid", 503, "auth_unavailable")
    return data

def mint_principal(
    path: str | Path,
    *,
    principal_id: str,
    scopes: list[str] | tuple[str, ...] | None = None,
    roles: list[str] | tuple[str, ...] | None = None,
    admin: bool = False,
) -> tuple[str, dict]:
    """Issue a new principal and write its token hash into the registry.

    Returns ``(plaintext_token, record)`` — the caller gets the plaintext exactly
    once; only the sha256 is persisted, matching how ``scripts/init.sh`` mints
    the default roster. The writer uses ``atomic_write_registry`` so a live
    ``TokenStore`` either sees the old table or the new one, never a torn file.
    """
    clean_id = str(principal_id or "").strip().lower()
    if not PRINCIPAL_ID_RE.fullmatch(clean_id):
        raise AuthError("invalid principal id", 400, "invalid_principal_id")
    granted = sorted(set(scopes if scopes is not None else ("read", "write")))
    unknown = [scope for scope in granted if scope not in KNOWN_SCOPES]
    if unknown:
        raise AuthError(
            f"unknown scope(s): {', '.join(unknown)}", 400, "invalid_scope"
        )
    data = _read_registry(path)
    if any(str(item.get("id", "")).strip().lower() == clean_id for item in data["principals"]):
        raise AuthError(f"principal already exists: {clean_id}", 409, "principal_exists")

    token = secrets.token_urlsafe(32)
    record = {
        "id": clean_id,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "scopes": granted,
        "roles": sorted(set(roles or ("authenticated",))),
        "admin": bool(admin),
    }
    data.setdefault("version", 1)
    data["principals"].append(record)
    atomic_write_registry(path, json.dumps(data, indent=2))
    return token, record

def revoke_principal(path: str | Path, *, principal_id: str) -> dict:
    """Remove a principal from the registry, invalidating its token immediately.

    Refuses to empty the registry: a registry with no principals makes
    ``TokenStore`` fail closed with 503, taking the whole service down. The
    caller must mint a replacement before revoking the last one.
    """
    clean_id = str(principal_id or "").strip().lower()
    if not clean_id:
        raise AuthError("invalid principal id", 400, "invalid_principal_id")
    data = _read_registry(path)
    remaining = [
        item for item in data["principals"]
        if str(item.get("id", "")).strip().lower() != clean_id
    ]
    if len(remaining) == len(data["principals"]):
        raise AuthError(f"principal not found: {clean_id}", 404, "principal_not_found")
    if not remaining:
        raise AuthError(
            "cannot revoke the last principal (service would fail closed)",
            409, "last_principal",
        )
    data["principals"] = remaining
    atomic_write_registry(path, json.dumps(data, indent=2))
    return {"principal_id": clean_id, "revoked": True, "remaining": len(remaining)}


class TokenStore:
    """Hot-reload a hash-only token registry and fail closed on errors."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._digest: str | None = None
        self._records: tuple[tuple[str, Principal], ...] = ()

    def _load(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError as exc:
            raise AuthError("authentication is not configured", 503, "auth_unavailable") from exc
        with self._lock:
            try:
                registry_bytes = self.path.read_bytes()
            except FileNotFoundError as exc:
                raise AuthError("authentication is not configured", 503, "auth_unavailable") from exc
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "token registry reload failed (path=%s): %s — "
                    "use mimir_v8.auth.atomic_write_registry for updates",
                    self.path, type(exc).__name__)
                raise AuthError("authentication registry is invalid", 503, "auth_unavailable") from exc

            registry_digest = hashlib.sha256(registry_bytes).hexdigest()
            if self._digest == registry_digest and self._records:
                return
            try:
                data = json.loads(registry_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                # Atomic replacements are still required; parsing the bytes just read ensures
                # invalid/partial writes fail closed rather than preserving stale authorization.
                logging.getLogger(__name__).warning(
                    "token registry reload failed (path=%s): %s — "
                    "use mimir_v8.auth.atomic_write_registry for updates",
                    self.path, type(exc).__name__)
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
            self._digest = registry_digest
            self._file_key = _file_key(stat)

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
