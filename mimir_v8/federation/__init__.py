"""Mímir v14.0 — 跨节点去中心化加密联邦 (spec 阶段四任务2).

Package surface:
- FederationService: CRDT event stream ledger + peer registry +
  encrypted-envelope export/ingest (the sync protocol unit).
- encrypt_envelope / decrypt_envelope: Fernet envelope helpers.
- FederationError: protocol/policy failures (Fail-Closed on anything
  unexpected — unknown peer, wrong key, tampered ciphertext).

Design (see spec 阶段四任务2): multi-home-server federation (N100,
desktop, cloud nodes) syncs through an append-only CRDT event stream.
Each fact change is one row carrying a lamport clock and the origin
node_id; conflicts merge last-writer-wins (lamport, then node_id as
the deterministic tiebreak — a total order, no divergence). Offline
nodes catch up by replaying the stream since their cursor; merge is
commutative so both directions converge to the same state.
"""

from .service import (
    FederationError,
    FederationService,
    decrypt_envelope,
    encrypt_envelope,
)

__all__ = [
    "FederationError",
    "FederationService",
    "decrypt_envelope",
    "encrypt_envelope",
]
