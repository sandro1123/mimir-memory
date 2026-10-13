"""三段式渐进披露的**服务端持有游标**（roadmap #13）。

issue #7 硬约束：游标由服务端持有不下发；跨步必须拒绝三种形态——
**任意 ID**、**改范围**、**过期 token**。

**为什么用 HMAC 签名而不是服务端会话表**：会话表本身会成为新的积压源
（Mímir 自己的 `human_review` 队列 6 件积压就是这句话的成本账单）。
签名 token 把状态放在**调用方拿不到明文**的载荷里，
服务端只需一把密钥即可验证，无需维护、清理、过期扫描。

**为什么不暴露内部 ID**：游标里只放**本次召回发放过的 ref 指纹**
（见 `issued_refs`），调用方无法凭游标构造任意 ID —— 这是"拒绝任意 ID"
的结构性保证，而不是靠事后校验。

叶子模块：只依赖标准库，被 api/query 反向依赖。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

#: 游标载荷版本。变更载荷结构必须升版本（旧游标据此可判为不认识）。
CURSOR_VERSION = 1


class CursorError(RuntimeError):
    """所有游标拒绝的基类——调用方可统一 except。"""


class CursorSignatureInvalid(CursorError):
    """签名不匹配（被篡改或密钥不符）。"""


class CursorScopeMismatch(CursorError):
    """游标不属于本次查询范围（改范围跨步）。"""


class CursorExpired(CursorError):
    """游标已过期。"""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret: bytes, body: str) -> str:
    return _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())


def mint_cursor(secret: bytes, *, scope: str, offset: int,
                issued_refs: list[str], ttl_seconds: int = 300) -> str:
    """签发一个不透明游标。

    `issued_refs` 是**本次召回发放过的**条目标识；后续展开只允许引用
    其中之一——这样"任意 ID"在结构上不可能。
    """
    payload = {
        "v": CURSOR_VERSION,
        "scope": scope,
        "offset": int(offset),
        "issued_refs": list(issued_refs),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"),
                            sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(secret, body)}"


def verify_cursor(secret: bytes, token: str, *, scope: str) -> dict:
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

    if payload.get("scope") != scope:
        raise CursorScopeMismatch("游标不属于本次查询范围")

    if int(payload.get("exp", 0)) < int(time.time()):
        raise CursorExpired("游标已过期")

    return payload
