#!/usr/bin/env python3
"""Mirasim chat-log collector: X1E room logs -> Mímir conversation ingestion.

Design: docs/plans/2026-11-25-mirasim-capture-design.md.

Mirasim stores every chat as append-only JSONL under
``~/.mirasim/chat/rooms/<room>/log/<first-seq>.jsonl``. This collector runs on
n100, copies the (small) rooms and users trees from X1E over LAN SSH, reads
only ``message`` lines past a per-room cursor, and posts them in batches to
``POST /v8/ingestion/conversations`` as the ingest-only ``mirasim`` principal.
Nothing on X1E is written; the cursor lives on n100 and only advances after
the API accepts a batch, so a missed run (X1E asleep, API down) is caught up
on the next one.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONNECTOR_TYPE = "external_agent"
CONNECTOR_ID = "mirasim-x1e"
OWNER = "mirasim"
BATCH_SIZE = 200
MAX_CONTENT_CHARS = 20000
COOLDOWN = timedelta(minutes=10)
BACKFILL = timedelta(days=7)
#: Rooms are named ``<kind>-<who>+<uid>``; the part before ``+`` decides inclusion.
INCLUDE_PREFIXES = ("dm-ai-", "dm-mx-")
INCLUDE_EXACT = frozenset({"general"})
#: Assistant turns that only report a failed model call carry no memory value.
SKIP_CID_PREFIXES = ("assist:fail",)

DEFAULT_REMOTE = "laptop-lan"
#: Relative to the remote user's home, so no machine path is baked in.
DEFAULT_REMOTE_CHAT = ".mirasim/chat"
DEFAULT_STATE = Path.home() / ".hermes" / "mimir" / "state" / "mirasim_cursor.json"
DEFAULT_TOKEN = Path.home() / ".hermes" / "mimir" / "secrets" / "clients" / "mirasim.token"


def room_included(room: str) -> bool:
    base = room.split("+", 1)[0]
    return base in INCLUDE_EXACT or base.startswith(INCLUDE_PREFIXES)


def parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def load_roles(users_dir: Path) -> dict[str, str]:
    """Map Mirasim user id -> conversation role from ``users/<id>/profile.json``."""
    roles: dict[str, str] = {}
    if not users_dir.is_dir():
        return roles
    for profile in users_dir.glob("*/profile.json"):
        try:
            kind = json.loads(profile.read_text(encoding="utf-8")).get("kind")
        except (OSError, ValueError):
            continue
        roles[profile.parent.name] = {"human": "user", "agent": "assistant"}.get(kind, "unknown")
    return roles


def card_text(card) -> str:
    """Readable text of a card: its title plus any plain string fields that look like prose."""
    if not isinstance(card, dict):
        return ""
    parts = [str(card.get(key)).strip() for key in ("title", "summary", "body", "text")
             if isinstance(card.get(key), str) and card.get(key).strip()]
    return "\n".join(dict.fromkeys(parts))


def read_room(room_dir: Path) -> tuple[list[dict], dict[int, dict]]:
    """Return ``(messages, edits_by_target)`` for one room, in seq order."""
    messages: list[dict] = []
    edits: dict[int, dict] = {}
    for log in sorted((room_dir / "log").glob("*.jsonl")):
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("kind")
            if kind == "message" and isinstance(row.get("seq"), int):
                messages.append(row)
            elif kind == "edit" and isinstance(row.get("target"), int):
                edits[row["target"]] = row  # last edit wins
    messages.sort(key=lambda row: row["seq"])
    return messages, edits


def select_new(messages: list[dict], edits: dict[int, dict], *, cursor: int | None,
               now: datetime, cooldown: timedelta = COOLDOWN,
               backfill: timedelta = BACKFILL) -> tuple[list[dict], dict]:
    """Messages to send this run, with edits applied; plus counters for the report.

    Stops at the first message still inside the cooldown so the cursor never
    jumps over a message that may yet be edited.
    """
    stats = {"skipped_fail": 0, "skipped_empty": 0, "skipped_backfill": 0, "late_edits": 0}
    cutoff = now - cooldown
    oldest = now - backfill
    picked: list[dict] = []
    for row in messages:
        seq = row["seq"]
        if cursor is not None and seq <= cursor:
            # Already sent. A recent edit most likely landed after the send;
            # v1 only reports it (design §4), it does not resend.
            edit_ts = parse_ts(edits[seq].get("ts")) if seq in edits else None
            if edit_ts is not None and edit_ts > cutoff - cooldown:
                stats["late_edits"] += 1
            continue
        ts = parse_ts(row.get("ts"))
        if ts is None or ts > cutoff:
            break
        if cursor is None and ts < oldest:
            stats["skipped_backfill"] += 1
            picked.append({"seq": seq, "skip": True})
            continue
        if str(row.get("cid", "")).startswith(SKIP_CID_PREFIXES):
            stats["skipped_fail"] += 1
            picked.append({"seq": seq, "skip": True})
            continue
        text = edits[seq].get("text") if seq in edits and isinstance(edits[seq].get("text"), str) else row.get("text")
        text = (text or "").strip()
        extra = card_text(row.get("card"))
        if extra and extra not in text:
            text = f"{text}\n{extra}".strip()
        if not text:
            stats["skipped_empty"] += 1
            picked.append({"seq": seq, "skip": True})
            continue
        picked.append({**row, "text": text[:MAX_CONTENT_CHARS], "edited": seq in edits})
    return picked, stats


def build_envelopes(room: str, picked: list[dict], roles: dict[str, str]) -> list[tuple[dict, int]]:
    """Batch the picked rows; each item is ``(envelope, last_seq_covered)``.

    Skipped rows still advance the cursor, so a batch's ``last_seq`` covers them.
    """
    out: list[tuple[dict, int]] = []
    batch: list[dict] = []
    first_seq = last_seq = None

    def flush():
        nonlocal batch, first_seq, last_seq
        if last_seq is None:
            return
        envelope = None
        if batch:
            envelope = {
                "connector_type": CONNECTOR_TYPE,
                "connector_id": CONNECTOR_ID,
                "session_id": room,
                "owner_principal": OWNER,
                "memory_mode": "observe",
                "retention_class": "short",
                "title": f"Mirasim {room.split('+', 1)[0]}",
                "started_at": batch[0]["created_at"],
                "ended_at": batch[-1]["created_at"],
                "metadata": {"source": "mirasim", "room": room},
                "messages": batch,
                "idempotency_key": f"mirasim:{room}:{first_seq}-{last_seq}",
            }
        out.append((envelope, last_seq))
        batch, first_seq, last_seq = [], None, None

    for row in picked:
        first_seq = row["seq"] if first_seq is None else first_seq
        last_seq = row["seq"]
        if not row.get("skip"):
            batch.append({
                "role": roles.get(row.get("by"), "unknown"),
                "content": row["text"],
                "created_at": row.get("ts"),
                "metadata": {"room": room, "seq": row["seq"], "speaker": row.get("name"),
                             "speaker_id": row.get("by"), "edited": row.get("edited", False)},
            })
        if len(batch) >= BATCH_SIZE:
            flush()
    flush()
    return out


def load_cursor(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}


def save_cursor(path: Path, cursor: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(cursor, indent=2, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def post(base_url: str, token: str, envelope: dict, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v8/ingestion/conversations",
        data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "X-Mimir-Agent": OWNER},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def fetch(remote: str, remote_chat: str, dest: Path) -> None:
    """Copy ``rooms`` and ``users`` from X1E; read-only on the remote side."""
    for part in ("rooms", "users"):
        subprocess.run(
            ["scp", "-q", "-r", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             f"{remote}:{remote_chat}/{part}", str(dest)],
            check=True, timeout=180,
        )


def collect(chat_dir: Path, cursor: dict[str, int], *, now: datetime, send, dry_run: bool) -> dict:
    """Process every included room; ``send(envelope)`` raises on failure."""
    roles = load_roles(chat_dir / "users")
    report = {"rooms": {}, "sent_batches": 0, "sent_messages": 0, "failed": []}
    for room_dir in sorted(p for p in (chat_dir / "rooms").iterdir() if p.is_dir()):
        room = room_dir.name
        if not room_included(room):
            continue
        messages, edits = read_room(room_dir)
        picked, stats = select_new(messages, edits, cursor=cursor.get(room), now=now)
        batches = build_envelopes(room, picked, roles)
        sent = 0
        for envelope, last_seq in batches:
            if envelope is not None and not dry_run:
                try:
                    send(envelope)
                except Exception as exc:  # noqa: BLE001 - stop this room; cursor stays put
                    report["failed"].append({"room": room, "error": f"{type(exc).__name__}: {exc}"[:240]})
                    break
            if envelope is not None:
                sent += len(envelope["messages"])
                report["sent_batches"] += 1
            if not dry_run:
                cursor[room] = last_seq
        report["sent_messages"] += sent
        report["rooms"][room] = {"messages": sent, **{k: v for k, v in stats.items() if v}}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remote", default=os.environ.get("MIRASIM_REMOTE", DEFAULT_REMOTE))
    parser.add_argument("--remote-chat", default=os.environ.get("MIRASIM_REMOTE_CHAT", DEFAULT_REMOTE_CHAT))
    parser.add_argument("--local-chat", help="read a local copy instead of fetching from X1E")
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("MIRASIM_STATE", DEFAULT_STATE)))
    parser.add_argument("--token-file", type=Path, default=Path(os.environ.get("MIRASIM_TOKEN_FILE", DEFAULT_TOKEN)))
    parser.add_argument("--base-url", default=os.environ.get("MIMIR_V8_URL", "http://127.0.0.1:8456"))
    parser.add_argument("--dry-run", action="store_true", help="report what would be sent; send nothing")
    args = parser.parse_args(argv)

    token = ""
    if not args.dry_run:
        try:
            token = args.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"error: cannot read token file {args.token_file}: {exc}", file=sys.stderr)
            return 2
    cursor = load_cursor(args.state)
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="mirasim-") as tmp:
        if args.local_chat:
            chat_dir = Path(args.local_chat)
        else:
            chat_dir = Path(tmp)
            try:
                fetch(args.remote, args.remote_chat, chat_dir)
            except (subprocess.SubprocessError, OSError) as exc:
                # X1E asleep or away from home: skip quietly, the next run catches up.
                print(json.dumps({"status": "skipped", "reason": f"fetch failed: {exc}"[:240]}))
                return 0
        report = collect(chat_dir, cursor, now=now, dry_run=args.dry_run,
                         send=lambda env: post(args.base_url, token, env))
    if not args.dry_run:
        save_cursor(args.state, cursor)
    report["status"] = "dry_run" if args.dry_run else ("error" if report["failed"] else "ok")
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
