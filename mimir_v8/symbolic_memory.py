"""Symbolic short-term memory for Mímir v11 — Mermaid canvas offload & drill-down.

Inspired by TencentDB Agent Memory's symbolic short-term memory:

  Full tool logs → offload to external block store → Mermaid symbol graph in context
  → Agent reasons over the graph → drills down via node_id when details matter.

This module is the "short-term" half of the two-tier memory system.
Long-term personalization (L0→L3) lives in core_memory.py and opinion.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .store import CanonicalStore, new_id, sha256_text, utc_now


V14_ADDITIVE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS symbolic_blocks (
        block_id TEXT PRIMARY KEY,
        owner_principal TEXT NOT NULL DEFAULT '',
        session_key TEXT NOT NULL,
        node_id TEXT NOT NULL,
        parent_node_id TEXT,
        block_type TEXT NOT NULL DEFAULT 'log',
        summary TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        mermaid_line TEXT,
        token_estimate INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(session_key, node_id)
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_symbolic_blocks_session
       ON symbolic_blocks(session_key, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_symbolic_blocks_node
       ON symbolic_blocks(node_id)""",
    """CREATE INDEX IF NOT EXISTS idx_symbolic_blocks_owner
       ON symbolic_blocks(owner_principal)""",
    """CREATE TABLE IF NOT EXISTS symbolic_canvases (
        canvas_id TEXT PRIMARY KEY,
        owner_principal TEXT NOT NULL DEFAULT '',
        session_key TEXT NOT NULL,
        mermaid TEXT NOT NULL,
        block_count INTEGER NOT NULL DEFAULT 0,
        total_token_estimate INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(session_key)
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_symbolic_canvases_owner
       ON symbolic_canvases(owner_principal)""",
    """CREATE TABLE IF NOT EXISTS code_symbols (
        symbol_id TEXT PRIMARY KEY,
        symbol_name TEXT NOT NULL,
        symbol_kind TEXT NOT NULL,
        file_path TEXT NOT NULL,
        line_number INTEGER,
        callers TEXT NOT NULL DEFAULT '[]',
        callees TEXT NOT NULL DEFAULT '[]',
        signature TEXT,
        doc_string TEXT,
        repo_url TEXT,
        indexed_at TEXT NOT NULL
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_code_symbols_name
       ON code_symbols(symbol_name)""",
    """CREATE INDEX IF NOT EXISTS idx_code_symbols_file
       ON code_symbols(file_path)""",
    "CREATE INDEX IF NOT EXISTS idx_code_symbols_kind ON code_symbols(symbol_kind)",
    "CREATE TABLE IF NOT EXISTS code_relations ("
    "  relation_id TEXT PRIMARY KEY,"
    "  caller_symbol_id TEXT NOT NULL REFERENCES code_symbols(symbol_id),"
    "  callee_symbol_id TEXT NOT NULL REFERENCES code_symbols(symbol_id),"
    "  call_type TEXT NOT NULL DEFAULT 'call',"
    "  file_path TEXT,"
    "  line_number INTEGER,"
    "  indexed_at TEXT NOT NULL"
    ") STRICT",
    "CREATE INDEX IF NOT EXISTS idx_code_relations_caller ON code_relations(caller_symbol_id)",
    "CREATE INDEX IF NOT EXISTS idx_code_relations_callee ON code_relations(callee_symbol_id)",
)


@dataclass
class SymbolicBlock:
    block_id: str
    owner_principal: str
    session_key: str
    node_id: str
    parent_node_id: str | None
    block_type: str
    summary: str
    raw_text: str
    mermaid_line: str | None
    token_estimate: int
    created_at: str


class SymbolicMemoryService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def offload_block(self, session_key: str, raw_text: str,
                      owner_principal: str = "",
                      block_type: str = "log",
                      parent_node_id: str | None = None,
                      summary: str | None = None,
                      max_tokens: int = 0) -> SymbolicBlock:
        raw_len = len(raw_text)
        token_estimate = max_tokens or raw_len // 4
        node_id = f"sym_{new_id()[:12]}"
        summary = summary or raw_text[:120].strip()
        now = utc_now()
        mermaid_line = self._make_mermaid_line(node_id, summary, block_type)
        block_id = new_id()
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO symbolic_blocks(
                    block_id, owner_principal, session_key, node_id, parent_node_id, block_type,
                    summary, raw_text, mermaid_line, token_estimate, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (block_id, owner_principal, session_key, node_id, parent_node_id, block_type,
                 summary, raw_text[:65536], mermaid_line, token_estimate, now),
            )
            self._upsert_canvas(conn, session_key, owner_principal, now)
        return SymbolicBlock(
            block_id=block_id, owner_principal=owner_principal, session_key=session_key, node_id=node_id,
            parent_node_id=parent_node_id, block_type=block_type,
            summary=summary, raw_text=raw_text, mermaid_line=mermaid_line,
            token_estimate=token_estimate, created_at=now,
        )

    def recall_block(self, node_id: str) -> dict | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM symbolic_blocks WHERE node_id=?", (node_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_canvas(self, session_key: str) -> str | None:
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT mermaid FROM symbolic_canvases WHERE session_key=?",
                (session_key,),
            ).fetchone()
            return row["mermaid"] if row else None

    def get_session_blocks(self, session_key: str, limit: int = 50) -> list[dict]:
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM symbolic_blocks WHERE session_key=? ORDER BY created_at DESC LIMIT ?",
                (session_key, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def _upsert_canvas(self, conn, session_key: str, owner_principal: str, now: str) -> None:
        block_rows = conn.execute(
            "SELECT node_id, mermaid_line, summary, block_type FROM symbolic_blocks "
            "WHERE session_key=? ORDER BY created_at ASC",
            (session_key,),
        ).fetchall()
        if not block_rows:
            return
        lines = ["graph LR"]
        for b in block_rows:
            lid = b["node_id"].replace("-", "_").replace(".", "_")
            label = b["summary"][:40].replace('"', "'")
            if b["mermaid_line"]:
                lines.append(b["mermaid_line"])
            else:
                lines.append(f'    {lid}["{label}"]')
        blk = block_rows[-1]
        prev_lid = blk["node_id"].replace("-", "_").replace(".", "_")
        for b in block_rows[:-1]:
            this_lid = b["node_id"].replace("-", "_").replace(".", "_")
            lines.append(f"    {this_lid} --> {prev_lid}")
            prev_lid = this_lid
        mermaid = "\n".join(lines)
        total_tokens = sum(
            (b["mermaid_line"] or "").count(" ") + 1 for b in block_rows
        ) * 4
        conn.execute(
            """INSERT OR REPLACE INTO symbolic_canvases(
                canvas_id, owner_principal, session_key, mermaid, block_count,
                total_token_estimate, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (new_id(), owner_principal, session_key, mermaid, len(block_rows),
             total_tokens, now, now),
        )

    def _make_mermaid_line(self, node_id: str, summary: str,
                           block_type: str) -> str:
        lid = node_id.replace("-", "_").replace(".", "_")
        label = summary[:40].replace('"', "'")
        shapes = {"log": f'{lid}["{label}"]',
                  "error": f'{lid}["{label}"]',
                  "result": f'{lid}["{label}"]',
                  "decision": f'{lid}{{"{label}"}}',
                  "tool_call": f'{lid}["{label}"]'}
        return shapes.get(block_type, shapes["log"])

    def index_code_symbol(self, symbol_name: str, symbol_kind: str,
                          file_path: str, line_number: int | None = None,
                          callers: list[str] | None = None,
                          callees: list[str] | None = None,
                          signature: str | None = None,
                          doc_string: str | None = None,
                          repo_url: str | None = None) -> str:
        symbol_id = new_id()
        now = utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO code_symbols(
                    symbol_id, symbol_name, symbol_kind, file_path, line_number,
                    callers, callees, signature, doc_string, repo_url, indexed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol_id, symbol_name, symbol_kind, file_path, line_number,
                 json.dumps(callers or []), json.dumps(callees or []),
                 signature, doc_string, repo_url, now),
            )
        return symbol_id

    def search_code_symbols(self, query: str, limit: int = 20) -> list[dict]:
        with self.store.transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM code_symbols WHERE
                   symbol_name LIKE ? OR signature LIKE ? OR doc_string LIKE ?
                   ORDER BY indexed_at DESC LIMIT ?""",
                (f"%{query}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_code_impact(self, symbol_id: str) -> dict:
        with self.store.transaction() as conn:
            symbol = conn.execute(
                "SELECT * FROM code_symbols WHERE symbol_id=?",
                (symbol_id,),
            ).fetchone()
            callees = conn.execute(
                "SELECT cs.* FROM code_relations cr JOIN code_symbols cs "
                "ON cr.callee_symbol_id=cs.symbol_id WHERE cr.caller_symbol_id=?",
                (symbol_id,),
            ).fetchall()
            callers = conn.execute(
                "SELECT cs.* FROM code_relations cr JOIN code_symbols cs "
                "ON cr.caller_symbol_id=cs.symbol_id WHERE cr.callee_symbol_id=?",
                (symbol_id,),
            ).fetchall()
            return {
                "symbol": dict(symbol) if symbol else None,
                "callees": [dict(r) for r in callees],
                "callers": [dict(r) for r in callers],
            }