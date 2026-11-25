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
MIMIR_HOST = os.environ.get("MIMIR_HOST", "127.0.0.1")
MIMIR_PORT = os.environ.get("MIMIR_PORT", "8456")
MIMIR_BASE = f"http://{MIMIR_HOST}:{MIMIR_PORT}"
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
    if source in ("resume", "compact"):
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

def extract_latest_turn(transcript_path: str, fallback_msg: str = "") -> tuple[str, str]:
    user_text, asst_text = "", fallback_msg or ""
    try:
        p = Path(transcript_path).expanduser()
        if not p.is_file():
            return "", asst_text
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - 300_000))
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()
    except Exception as e:
        log(f"transcript read fail: {e}")
        return "", asst_text

    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rtype = rec.get("type")
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
            content = "\n".join(parts)
        if not isinstance(content, str) or not content.strip():
            continue
        if rtype == "assistant" and not asst_text:
            asst_text = content.strip()
        elif rtype == "user":
            c_str = content.strip()
            if c_str.startswith("{") or "<local-command" in c_str:
                continue
            user_text = c_str
            if asst_text:
                break
    return user_text, asst_text


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
