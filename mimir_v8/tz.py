"""Display/report timezone for Mímir.

Historically every module hard-coded ``UTC+8``. That default is kept for
backward compatibility, but deployments elsewhere can now set ``MIMIR_TZ``
to an IANA name (``Europe/Berlin``) or a fixed offset (``+02:00``).
Canonical timestamps in the store remain UTC; this only affects
human-facing dates (reports, review queues, collector stamps).
"""

from __future__ import annotations

import os
import re
from datetime import timedelta, timezone, tzinfo

_DEFAULT = timezone(timedelta(hours=8))
_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")


def configured_tz() -> tzinfo:
    raw = os.environ.get("MIMIR_TZ", "").strip()
    if not raw:
        return _DEFAULT
    m = _OFFSET_RE.match(raw)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(raw)
    except Exception:
        return _DEFAULT


TZ = configured_tz()
