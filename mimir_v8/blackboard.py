"""Mímir v13.0 — 多 Agent 共享工作黑板 (Shared Working Memory Blackboard).

spec 阶段三任务1：跨 Agent 秒级共享瞬态任务上下文。

设计要点：
- 黑板是瞬态层：独立 SQLite 文件（blackboard.db），条目**不写
  memory_events**——只有任务结束 distill 沉淀为长期事实时才走
  CanonicalStore.create_fact 的既有事件流。主链不被高频协同噪声污染。
- 高并发：WAL 模式 + busy_timeout，多进程可同时读写。
- 两个终结出口：distill（摘要沉淀为 pattern 事实）与 destroy（安全
  销毁——清空全部条目与 board 行，销毁本身可观测）。
- ACL：board 参与者集之外的 agent 一律 Fail-Closed。
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .store import CanonicalStore, new_id, utc_now


BLACKBOARD_SQL = """
CREATE TABLE IF NOT EXISTS blackboards (
    board_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    participants TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','distilled','destroyed')),
    created_at TEXT NOT NULL,
    ended_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS blackboard_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id TEXT NOT NULL REFERENCES blackboards(board_id),
    entry_id TEXT NOT NULL UNIQUE,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_blackboard_entries_board
    ON blackboard_entries(board_id, seq);
"""


class BlackboardError(Exception):
    """Policy/validation failure on blackboard operations."""


@dataclass(frozen=True)
class CreateBoard:
    board_id: str
    title: str
    participants: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostEntry:
    board_id: str
    content: str
    author: str
    entry_id: str = field(default_factory=new_id)


class BlackboardService:
    """Task scratchpad over a dedicated SQLite file.

    Entries never touch the immutable event stream; distill() is the single
    gateway that materializes a long-term pattern fact through the normal
    CanonicalStore.create_fact path.
    """

    def __init__(self, store: CanonicalStore, database: str | Path):
        self.store = store
        self.path = Path(database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as connection:
            connection.executescript(BLACKBOARD_SQL)
            connection.commit()

    def _require_active_board(self, connection, board_id: str) -> sqlite3.Row:
        board = connection.execute(
            "SELECT * FROM blackboards WHERE board_id=?", (board_id,)
        ).fetchone()
        if board is None:
            raise BlackboardError(f"unknown board: {board_id}")
        if board["status"] != "active":
            raise BlackboardError(
                f"board {board_id} is {board['status']} — no further writes"
            )
        return board

    def _authorize(self, board: sqlite3.Row, actor_principal: str) -> None:
        participants = tuple(board["participants"].split(","))
        if actor_principal not in participants:
            raise BlackboardError(
                f"principal {actor_principal} is not a participant of {board['board_id']}"
            )

    def create_board(self, command: CreateBoard) -> dict:
        board_id = command.board_id.strip()
        title = command.title.strip()
        if not board_id or not title:
            raise BlackboardError("board_id and title are required")
        participants = tuple(dict.fromkeys(command.participants))
        if not participants:
            raise BlackboardError("at least one participant is required")
        now = utc_now()
        with contextlib.closing(self._connect()) as connection:
            try:
                connection.execute(
                    "INSERT INTO blackboards(board_id, title, participants, status, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (board_id, title, ",".join(participants), "active", now),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                raise BlackboardError(f"board already exists: {board_id}") from error
        return {
            "board_id": board_id,
            "title": title,
            "participants": list(participants),
            "status": "active",
            "created_at": now,
        }

    def post_entry(self, command: PostEntry, *, actor_principal: str) -> dict:
        if actor_principal != command.author:
            raise BlackboardError("author must match the acting principal")
        content = command.content.strip()
        if not content:
            raise BlackboardError("entry content must not be empty")
        now = utc_now()
        with contextlib.closing(self._connect()) as connection:
            board = self._require_active_board(connection, command.board_id)
            self._authorize(board, actor_principal)
            cursor = connection.execute(
                "INSERT INTO blackboard_entries(board_id, entry_id, author, content, created_at)"
                " VALUES(?,?,?,?,?)",
                (command.board_id, command.entry_id, command.author, content, now),
            )
            connection.commit()
            return {
                "entry_id": command.entry_id,
                "board_id": command.board_id,
                "seq": cursor.lastrowid,
                "author": command.author,
                "content": content,
                "created_at": now,
            }

    def list_entries(self, board_id: str, *, actor_principal: str) -> list[dict]:
        with contextlib.closing(self._connect()) as connection:
            board = connection.execute(
                "SELECT * FROM blackboards WHERE board_id=?", (board_id,)
            ).fetchone()
            if board is None:
                raise BlackboardError(f"unknown board: {board_id}")
            if board["status"] == "destroyed":
                raise BlackboardError(f"board {board_id} was destroyed")
            self._authorize(board, actor_principal)
            rows = connection.execute(
                "SELECT * FROM blackboard_entries WHERE board_id=? ORDER BY seq",
                (board_id,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def read_since(self, board_id: str, *, actor_principal: str, after_seq: int) -> list[dict]:
        """Incremental pull: entries with seq > after_seq (協同增量拉取)."""
        entries = self.list_entries(board_id, actor_principal=actor_principal)
        return [entry for entry in entries if entry["seq"] > after_seq]

    def distill(
        self, board_id: str, *, actor_principal: str, summary: str
    ) -> dict:
        """End the board by materializing its distilled essence as a long-term
        pattern fact — through the normal canonical event stream."""
        summary = summary.strip()
        if not summary:
            raise BlackboardError("distill summary must not be empty")
        with contextlib.closing(self._connect()) as connection:
            board = self._require_active_board(connection, board_id)
            self._authorize(board, actor_principal)
            entries = connection.execute(
                "SELECT author, content, created_at FROM blackboard_entries"
                " WHERE board_id=? ORDER BY seq",
                (board_id,),
            ).fetchall()
        now = utc_now()
        fact = self.store.create_fact(
            _PatternFact(
                content=summary,
                summary=summary[:120],
                owner_principal=actor_principal,
            ),
            actor_principal=actor_principal,
        )
        with contextlib.closing(self._connect()) as connection:
            connection.execute(
                "UPDATE blackboards SET status='distilled', ended_at=? WHERE board_id=?",
                (now, board_id),
            )
            connection.commit()
        return {
            "board_id": board_id,
            "status": "distilled",
            "fact_id": fact["fact_id"],
            "distilled_entries": len(entries),
        }

    def destroy(self, board_id: str, *, actor_principal: str, reason: str) -> dict:
        """End the board by safely wiping all entries (no fact materialized)."""
        reason = reason.strip()
        if not reason:
            raise BlackboardError("destroy reason must not be empty")
        now = utc_now()
        with contextlib.closing(self._connect()) as connection:
            board = self._require_active_board(connection, board_id)
            self._authorize(board, actor_principal)
            wiped = connection.execute(
                "DELETE FROM blackboard_entries WHERE board_id=?", (board_id,)
            ).rowcount
            connection.execute(
                "UPDATE blackboards SET status='destroyed', ended_at=? WHERE board_id=?",
                (now, board_id),
            )
            connection.commit()
        return {
            "board_id": board_id,
            "status": "destroyed",
            "destroyed_entries": wiped,
            "reason": reason,
            "ended_at": now,
        }

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict:
        return {
            "entry_id": row["entry_id"],
            "seq": row["seq"],
            "board_id": row["board_id"],
            "author": row["author"],
            "content": row["content"],
            "created_at": row["created_at"],
        }


from .schema import CreateFact  # noqa: E402  (kept late to avoid cycle at import)


class _PatternFact(CreateFact):
    """Distill target: a pattern fact with sane defaults."""

    def __init__(self, content: str, summary: str, owner_principal: str):
        super().__init__(
            content=content,
            summary=summary,
            owner_principal=owner_principal,
            domain="knowledge",
            fact_type="pattern",
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status="unreviewed",
        )
