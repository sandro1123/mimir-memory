#!/usr/bin/env python3
"""
Mímir A-Tier Hook Adapter for Claude Code / Bridge
Supports:
  - SessionStart: Injects identity-scoped iron rules and user preferences (read-only bootstrap)
  - SessionEnd / Stop / PreCompact: Ingests dialogue transcript and auto-crystallizes STM candidate
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

def _resolve_agent_id() -> str:
    """Identity must be explicit: MIMIR_AGENT_ID, or "" when it is unset.

    There is no detection and no default. LARK_CHANNEL / LARK_CHANNEL_HOME say
    nothing about which agent this is (Odin runs under the same bridge), so they
    are ignored. An empty result makes main() log an error and do nothing.
    """
    return os.environ.get("MIMIR_AGENT_ID", "").strip().lower()


AGENT_ID = _resolve_agent_id()
MIMIR_HOST = os.environ.get("MIMIR_HOST", "127.0.0.1").strip()
MIMIR_PORT = os.environ.get("MIMIR_PORT", "8456").strip()
MIMIR_BASE = f"http://{MIMIR_HOST}:{MIMIR_PORT}"
# Issue #72: requests carry the agent token and session text, so the target is
# pinned. Loopback is always allowed; any other host must be listed in a file
# next to the tokens — an environment variable alone cannot widen the set.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TRUSTED_HOSTS_FILE = Path.home() / ".hermes" / "mimir" / "secrets" / "clients" / "trusted_hosts"


def _trusted_hosts() -> frozenset[str]:
    hosts = set(_LOOPBACK_HOSTS)
    try:
        for line in TRUSTED_HOSTS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line:
                hosts.add(line)
    except OSError:
        pass
    return frozenset(hosts)


def host_is_trusted(host: str | None = None, port: str | None = None) -> bool:
    host = (MIMIR_HOST if host is None else host).strip().lower()
    port = MIMIR_PORT if port is None else port
    return port.isdigit() and host in _trusted_hosts()
# Token follows identity: secrets/clients/<agent>.token. No fallback to another
# agent's token — a missing token logs an error and the hook does nothing.
MIMIR_TOKEN_FILE = Path(os.environ.get(
    "MIMIR_TOKEN_FILE",
    str(Path.home() / ".hermes" / "mimir" / "secrets" / "clients" / f"{AGENT_ID}.token"),
))
RESUME_INJECT_INTERVAL_S = 6 * 3600  # resume/compact: re-inject at most every 6h per session

STATE_DIR = Path.home() / ".claude" / "hooks"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = STATE_DIR / "mimir_auto_capture.log"
SEEN_FILE = STATE_DIR / "mimir_seen.json"

MIN_TURN_CHARS = 40
MAX_TURN_CHARS = 10000
INJECT_MAX_CHARS = 1600


def log(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{AGENT_ID}] {msg}\n")
        if LOG_FILE.stat().st_size > 512_000:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-2000:]
            LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _token() -> str:
    if not MIMIR_TOKEN_FILE.is_file():
        return ""
    return MIMIR_TOKEN_FILE.read_text(encoding="utf-8").strip()


def mimir_get(path: str, timeout: int = 10) -> dict | None:
    if not host_is_trusted():
        log(f"error: MIMIR_HOST {MIMIR_HOST!r} is not trusted (see {TRUSTED_HOSTS_FILE}); no request sent")
        return None
    tok = _token()
    if not tok:
        log("err: token file not found")
        return None
    req = urllib.request.Request(
        f"{MIMIR_BASE}{path}",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"mimir_get {path} failed: {e}")
        return None


def mimir_post(path: str, body: dict, timeout: int = 15) -> dict | None:
    if not host_is_trusted():
        log(f"error: MIMIR_HOST {MIMIR_HOST!r} is not trusted (see {TRUSTED_HOSTS_FILE}); no request sent")
        return None
    tok = _token()
    if not tok:
        log("err: token file not found")
        return None
    req = urllib.request.Request(
        f"{MIMIR_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"mimir_post {path} failed: {e}")
        return None


def load_seen() -> dict:
    try:
        if SEEN_FILE.is_file():
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_seen(seen: dict) -> None:
    try:
        cutoff = time.time() - 7 * 86400
        cleaned = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v > cutoff}
        tmp = SEEN_FILE.with_name(SEEN_FILE.name + ".tmp")
        tmp.write_text(json.dumps(cleaned, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SEEN_FILE)
    except Exception:
        pass


# ----------------------------------------------------------------- SessionStart
def handle_session_start(payload: dict) -> None:
    source = str(payload.get("source") or "")
    # compact drops prior context, so always re-inject (2026-11-26 Odin amnesia fix);
    # only resume is throttled.
    if source == "resume":
        # The bridge resumes the same session every turn, so skipping resume
        # meant Thor *never* got its bootstrap (2026-11-24 audit). Inject on
        # resume too, throttled per session so it isn't repeated every turn.
        sid = str(payload.get("session_id") or "unknown_session")
        seen = load_seen()
        key = f"inject:{AGENT_ID}:{sid}"
        last = seen.get(key)
        if isinstance(last, (int, float)) and time.time() - last < RESUME_INJECT_INTERVAL_S:
            log(f"[SessionStart] skip: source={source}, injected {int(time.time() - last)}s ago")
            return
        seen[key] = time.time()
        save_seen(seen)

    data = mimir_get("/v8/hooks/session-start", timeout=8)
    if not data:
        log("[SessionStart] empty or failed bootstrap from mimir")
        return

    iron_rules = data.get("iron_rules", [])
    user_prefs = data.get("user_prefs", [])

    lines = ["【Mímir 记忆中枢 · 核心基线与偏好预置】"]
    if iron_rules:
        lines.append("■ 核心铁律（系统安全与运行红线，严格遵守）：")
        for r in iron_rules[:6]:
            content = r.get("content") or r.get("summary") or ""
            content = content.strip().replace("\n", " ")
            if content:
                lines.append(f"- {content[:140]}")

    if user_prefs:
        lines.append("\n■ 主人工作习惯与偏好：")
        for p in user_prefs[:5]:
            content = p.get("content") or p.get("summary") or ""
            content = content.strip().replace("\n", " ")
            if content:
                lines.append(f"- {content[:130]}")

    prompt = "\n".join(lines)
    if len(prompt) > INJECT_MAX_CHARS:
        prompt = prompt[:INJECT_MAX_CHARS] + "…"

    log(f"[SessionStart] injected {len(prompt)} chars (rules={len(iron_rules)}, prefs={len(user_prefs)})")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            # Claude Code reads `additionalContext`; `injectedPrompt` is kept
            # only for older bridge builds that may have looked for it.
            "additionalContext": prompt,
            "injectedPrompt": prompt,
        }
    }
    print(json.dumps(output))


# ----------------------------------------------------------- SessionEnd / Stop
_BRIDGE_BLOCK_RE = re.compile(
    r"<(bridge_context|bridge_instructions|system-reminder|calendar_hint|"
    r"skill_preload_outcome|task-notification)>.*?</\1>",
    re.DOTALL,
)
_ROUTE_RE = re.compile(r"\[ACP_ROUTE:[^\]]*\]|\[SYSTEM EVENT\][^\n]*|\[Client tool calling[^\n]*")
_USER_INPUT_RE = re.compile(r"<user_input>\s*(\{.*?\})\s*</user_input>", re.DOTALL)


def clean_turn_text(text: str) -> str:
    """Strip bridge/system scaffolding so only what the human said is captured.

    2026-11-24 audit: candidates were crystallized from raw
    ``user: <bridge_context>{"chatId"...`` blobs (some judged noise, some
    stuck in human_review). Prefer the bridge's <user_input>{"text": ...}.
    """
    if not text:
        return ""
    m = _USER_INPUT_RE.search(text)
    if m:
        try:
            inner = json.loads(m.group(1))
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                return inner["text"].strip()
        except ValueError:
            pass
    text = _BRIDGE_BLOCK_RE.sub("", text)
    text = _ROUTE_RE.sub("", text)
    text = re.sub(r"</?user_(message|input)>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

#: Bounded reverse scan of the transcript (#81). A single large trailing
#: tool_result used to fill the old fixed 300 KB tail window and hide the turn.
_TAIL_CHUNK_BYTES = 256 * 1024
_TAIL_MAX_SCAN_BYTES = 32 * 1024 * 1024
_TAIL_MAX_RECORD_BYTES = 2 * 1024 * 1024
#: Harness-injected "user" records that are not something the human typed.
_NON_HUMAN_PREFIXES = ("{", "<task-notification")


def _iter_lines_reversed(path: Path):
    """Yield complete JSONL lines newest first, reading backwards in chunks.

    Stops after ``_TAIL_MAX_SCAN_BYTES``; a line longer than
    ``_TAIL_MAX_RECORD_BYTES`` is skipped instead of buffered.
    """
    pos = path.stat().st_size
    scanned = 0
    buf = b""
    skipping = False
    with path.open("rb") as f:
        while pos > 0:
            if scanned >= _TAIL_MAX_SCAN_BYTES:
                log(f"transcript scan limit reached ({scanned} bytes) before a user turn")
                return
            n = min(_TAIL_CHUNK_BYTES, pos, _TAIL_MAX_SCAN_BYTES - scanned)
            pos -= n
            f.seek(pos)
            parts = (f.read(n) + buf).split(b"\n")
            scanned += n
            if skipping:
                if len(parts) == 1:  # still inside the oversized record
                    buf = b""
                    continue
                parts = parts[:-1]  # drop its first fragment
                skipping = False
            buf = parts[0]
            for line in reversed(parts[1:]):
                yield line.decode("utf-8", errors="ignore")
            if len(buf) > _TAIL_MAX_RECORD_BYTES:
                log(f"transcript record oversized (>{_TAIL_MAX_RECORD_BYTES} bytes), skipped")
                buf = b""
                skipping = True
    if buf and not skipping:
        yield buf.decode("utf-8", errors="ignore")


def _record_text(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "\n".join(c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
    return content.strip() if isinstance(content, str) else ""


def extract_latest_turn(transcript_path: str, fallback_msg: str = "") -> tuple[str, str]:
    """Return the newest human user message and the reply to it.

    Assistant text written before that user message belongs to an earlier
    turn and is never paired with it. ``fallback_msg`` (the hook's
    ``last_assistant_message``) wins over the transcript when given.
    """
    user_text, asst_text = "", ""
    try:
        p = Path(transcript_path).expanduser()
        if not p.is_file():
            return "", fallback_msg or ""
        for line in _iter_lines_reversed(p):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isMeta"):
                continue
            text = _record_text(rec)
            if not text:
                continue
            rtype = rec.get("type")
            if rtype == "assistant" and not asst_text:
                asst_text = text
            elif rtype == "user":
                if text.startswith(_NON_HUMAN_PREFIXES) or "<local-command" in text:
                    continue
                user_text = text
                break
    except Exception as e:
        log(f"transcript read fail: {e}")
        return "", fallback_msg or ""
    return user_text, fallback_msg or asst_text


def handle_session_end(payload: dict, event: str) -> None:
    session_id = payload.get("session_id", "unknown_session")
    transcript_path = payload.get("transcript_path", "")
    last_asst_msg = payload.get("last_assistant_message", "") or ""

    user_text, asst_text = extract_latest_turn(transcript_path, last_asst_msg)
    user_text, asst_text = clean_turn_text(user_text), clean_turn_text(asst_text)
    combined = f"{user_text}\n{asst_text}".strip()

    if len(combined) < MIN_TURN_CHARS:
        log(f"[{event}] skip: turn too short ({len(combined)} chars)")
        return
    if len(combined) > MAX_TURN_CHARS:
        combined = combined[:MAX_TURN_CHARS]

    # Content hash for deduplication
    h = hashlib.sha256(combined.encode("utf-8", errors="ignore")).hexdigest()[:16]
    key = f"{session_id}:{h}"
    seen = load_seen()
    if key in seen:
        log(f"[{event}] skip: already submitted {key}")
        return

    # Construct conversation envelope payload for /v8/hooks/session-end
    messages = []
    if user_text:
        messages.append({"role": "user", "content": user_text[:3000]})
    if asst_text:
        messages.append({"role": "assistant", "content": asst_text[:5000]})

    if not messages:
        return

    envelope = {
        "connector_type": "external_agent",
        "connector_id": f"{AGENT_ID}-bridge",
        "session_id": session_id,
        "owner_principal": AGENT_ID,
        "memory_mode": "observe",
        "retention_class": "short",
        "messages": messages,
        "idempotency_key": f"{AGENT_ID}:{session_id}:{h}",
    }

    resp = mimir_post("/v8/hooks/session-end", envelope, timeout=12)
    if resp and "candidate_id" in resp:
        log(f"[{event}] STM candidate created: {resp.get('candidate_id')} (run={resp.get('run_id')[:8]})")
        seen[key] = time.time()
        save_seen(seen)
    else:
        log(f"[{event}] session-end resp: {resp}")


def main() -> None:
    if not AGENT_ID:
        log("error: MIMIR_AGENT_ID is not set; hook is inactive (no identity, no network calls)")
        return
    if not host_is_trusted():
        log(f"error: MIMIR_HOST {MIMIR_HOST!r} is not loopback or listed in {TRUSTED_HOSTS_FILE}; hook is inactive")
        return
    raw_input = ""
    try:
        raw_input = sys.stdin.read().strip()
    except Exception:
        pass

    payload = {}
    if raw_input:
        try:
            payload = json.loads(raw_input)
        except Exception:
            pass

    event = payload.get("hook_event_name", "")
    if not event and len(sys.argv) > 1:
        event = sys.argv[1]

    if event == "SessionStart":
        handle_session_start(payload)
    elif event in ("SessionEnd", "Stop", "PreCompact"):
        handle_session_end(payload, event)
    else:
        log(f"unhandled event: {event}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
    sys.exit(0)
