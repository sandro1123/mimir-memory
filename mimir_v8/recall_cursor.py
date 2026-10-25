"""三段式渐进披露的签名游标（roadmap #13）。

游标由调用方持有；HMAC 提供完整性，不提供保密性。持有游标的人可解码
其载荷，因此不得在其中放秘密。展开时还必须绑定认证主体，并重新检查
当前 ACL 和事实状态；签名本身不能替代授权。

游标仅允许展开本次召回发放过的事实 ID，拒绝调用方自行指定任意 ID。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

#: 游标载荷版本。变更载荷结构必须升版本（旧游标据此可判为不认识）。
CURSOR_VERSION = 2


class CursorError(RuntimeError):
    """所有游标拒绝的基类——调用方可统一 except。"""


class CursorSignatureInvalid(CursorError):
    """签名不匹配（被篡改或密钥不符）。"""


class CursorScopeMismatch(CursorError):
    """游标不属于本次查询范围（改范围跨步）。"""


class CursorExpired(CursorError):
    """游标已过期。"""


class CursorPrincipalMismatch(CursorError):
    """游标不属于当前认证主体。"""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret: bytes, body: str) -> str:
    return _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())


def mint_cursor(secret: bytes, *, scope: str, principal_id: str, offset: int,
                issued_refs: list[str], ttl_seconds: int = 300) -> str:
    """签发一个不透明游标。

    `issued_refs` 是**本次召回发放过的**条目标识；后续展开只允许引用
    其中之一——这样"任意 ID"在结构上不可能。
    """
    payload = {
        "v": CURSOR_VERSION,
        "scope": scope,
        "principal_id": principal_id,
        "offset": int(offset),
        "issued_refs": list(issued_refs),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"),
                            sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(secret, body)}"


def verify_cursor(secret: bytes, token: str, *, scope: str | None = None,
                  principal_id: str | None = None) -> dict:
    """验证并解出游标载荷。任何不符即抛 CursorError 子类。"""
    if not isinstance(token, str) or "." not in token:
        raise CursorSignatureInvalid("游标格式非法")

    body, _, sig = token.rpartition(".")
    # 先验签名，再解内容——签名不过就不该信任任何字段（含 scope/exp）
    if not hmac.compare_digest(sig, _sign(secret, body)):
        raise CursorSignatureInvalid("游标签名不匹配")

    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:  # noqa: BLE001 - 一律归为签名无效族
        raise CursorSignatureInvalid(f"游标载荷不可解析: {exc}") from exc

    if payload.get("v") != CURSOR_VERSION:
        raise CursorSignatureInvalid(
            f"游标版本不认识: {payload.get('v')!r}")

    if scope is not None and payload.get("scope") != scope:
        raise CursorScopeMismatch("游标不属于本次查询范围")
    if principal_id is not None and payload.get("principal_id") != principal_id:
        raise CursorPrincipalMismatch("游标不属于当前认证主体")

    if int(payload.get("exp", 0)) < int(time.time()):
        raise CursorExpired("游标已过期")

    return payload
