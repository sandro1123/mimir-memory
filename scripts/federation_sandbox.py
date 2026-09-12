#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mímir 联邦沙箱 — 1.0 线B三段式第一段（同机双实例实测）。

用法:
  python scripts/federation_sandbox.py            # 人类可读逐项输出
  python scripts/federation_sandbox.py --json     # 纯 JSON 回执
  python scripts/federation_sandbox.py --out PATH  # 回执另存文件

两个一次性库真跑全生命周期：注册 → 发布 → 双向导出/回灌 → LWW 收敛 →
幂等重放 → 删除墓碑，外加 P0-L 硬化五点的活体验证（密钥持久化/收件人
校验/署名三面/lamport 界/载荷上限）。任何一步红即退出码 1。

联邦通电三段式的第一段证据（第二段=Tailscale 真双节点，第三段=生产
单向接入）。回执即 issue #5 联邦摘牌材料的前半。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.federation import FederationService, encrypt_envelope  # noqa: E402
from mimir_v8.federation.service import FederationError  # noqa: E402
from mimir_v8.store import CanonicalStore  # noqa: E402


def _mk_node(root: Path, name: str) -> FederationService:
    return FederationService(CanonicalStore(root / f"{name}.db"), node_id=name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mímir federation two-instance sandbox")
    ap.add_argument("--json", action="store_true", help="print machine-readable receipt only")
    ap.add_argument("--out", default=None, help="also write receipt to this path")
    args = ap.parse_args(argv)

    results: list[dict] = []

    def step(name: str, fn) -> None:
        try:
            detail = fn()
            results.append({"check": name, "ok": True, "detail": str(detail or "")})
            if not args.json:
                print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
        except Exception as exc:
            results.append({"check": name, "ok": False,
                            "detail": f"{type(exc).__name__}: {exc}"})
            if not args.json:
                print(f"  FAIL  {name} — {type(exc).__name__}: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = _mk_node(root, "n100")
        b = _mk_node(root, "desktop")

        def s01_key_persistence():
            a2 = FederationService(a.store, node_id="n100")  # 模拟服务重启
            assert a2.public_key == a.public_key, "restart rotated the key"
            return "key stable across restart (peers never orphaned)"

        def s02_peer_registration():
            a.register_peer("desktop", b.public_key)
            b.register_peer("n100", a.public_key)
            return f"a sees {len(a.list_peers())} peer, b sees {len(b.list_peers())} peer"

        def s03_publish():
            a.append_event({"key": "shared/skill/k8s-drain", "lamport": 3,
                            "node_id": "n100", "op": "set", "value": "runbook-v1"})
            a.append_event({"key": "shared/pref/theme", "lamport": 1,
                            "node_id": "n100", "op": "set", "value": "dark"})
            a.append_event({"key": "shared/skill/k8s-drain", "lamport": 7,
                            "node_id": "n100", "op": "set", "value": "tie-a"})
            b.append_event({"key": "shared/skill/k8s-drain", "lamport": 5,
                            "node_id": "desktop", "op": "set", "value": "runbook-v2"})
            b.append_event({"key": "shared/skill/k8s-drain", "lamport": 7,
                            "node_id": "desktop", "op": "set", "value": "tie-b"})
            return "a published 3 events, b published 2 (conflict key included)"

        def s04_bidirectional_sync():
            r1 = b.ingest_envelope(a.export_events(since=0, to_peer="desktop"))
            r2 = a.ingest_envelope(b.export_events(since=0, to_peer="n100"))
            return f"a->b applied={r1['applied']}, b->a applied={r2['applied']}"

        def s05_lww_convergence():
            sa = a.crdt_state("shared/skill/k8s-drain")
            sb = b.crdt_state("shared/skill/k8s-drain")
            # lamport 7 tie -> node_id DESC -> n100 wins -> "tie-a";
            # both nodes must fold the identical winner regardless of arrival order
            assert sa["value"] == sb["value"] == "tie-a", (sa, sb)
            assert sa["lamport"] == sb["lamport"] == 7, (sa, sb)
            ta = a.crdt_state("shared/pref/theme")
            tb = b.crdt_state("shared/pref/theme")
            assert ta["value"] == tb["value"] == "dark", (ta, tb)
            return "both nodes fold tie-a @7; theme=dark propagated"

        def s06_idempotent_replay():
            r = b.ingest_envelope(a.export_events(since=0, to_peer="desktop"))
            assert r["applied"] == 0, r
            return "re-ingest is a no-op (applied=0)"

        def s07_delete_tombstone():
            b.append_event({"key": "shared/pref/theme", "lamport": 9,
                            "node_id": "desktop", "op": "delete"})
            a.ingest_envelope(b.export_events(since=0, to_peer="n100"))
            sa = a.crdt_state("shared/pref/theme")
            sb = b.crdt_state("shared/pref/theme")
            assert sa["op"] == sb["op"] == "delete", (sa, sb)
            return "delete folded on both nodes (tombstone wins by lamport)"

        def s08_to_peer_guard():
            env = b.export_events(since=0, to_peer="n100")
            bad = dict(env)
            bad["to_peer"] = "node-x"
            try:
                a.ingest_envelope(bad)
                raise AssertionError("misaddressed envelope accepted")
            except FederationError:
                return "misaddressed envelope rejected (fail-closed)"

        def s09_forged_signer_guard():
            ghost = {"key": "shared/x", "lamport": 1,
                     "node_id": "ghost-node", "op": "set", "value": "pwn"}
            payload = {"from_node": "desktop", "to_peer": "n100", "events": [ghost]}
            env = {"from_node": "desktop", "to_peer": "n100",
                   "ciphertext": encrypt_envelope(payload, b.public_key)}
            try:
                a.ingest_envelope(env)
                raise AssertionError("forged signer accepted")
            except FederationError:
                return "unknown signer rejected (three-face gate)"

        def s10_lamport_bound():
            try:
                a.append_event({"key": "x", "lamport": 10**8,
                                "node_id": "n100", "op": "set", "value": "bomb"})
                raise AssertionError("lamport bomb accepted")
            except FederationError:
                return "lamport >= 1e8 rejected (no permanent-delete attack)"

        def s11_envelope_cap():
            events = [{"key": f"flood/{i}", "lamport": 1,
                       "node_id": "desktop", "op": "set", "value": "x"}
                      for i in range(5001)]
            payload = {"from_node": "desktop", "to_peer": "n100", "events": events}
            env = {"from_node": "desktop", "to_peer": "n100",
                   "ciphertext": encrypt_envelope(payload, b.public_key)}
            try:
                a.ingest_envelope(env)
                raise AssertionError("oversized envelope accepted")
            except FederationError:
                return "envelope >5000 events rejected"

        for name, fn in [
            ("key_persistence", s01_key_persistence),
            ("peer_registration", s02_peer_registration),
            ("publish", s03_publish),
            ("bidirectional_sync", s04_bidirectional_sync),
            ("lww_convergence", s05_lww_convergence),
            ("idempotent_replay", s06_idempotent_replay),
            ("delete_tombstone", s07_delete_tombstone),
            ("to_peer_guard", s08_to_peer_guard),
            ("forged_signer_guard", s09_forged_signer_guard),
            ("lamport_bound", s10_lamport_bound),
            ("envelope_cap", s11_envelope_cap),
        ]:
            step(name, fn)

    ok = all(r["ok"] for r in results)
    receipt = {
        "sandbox": "v15-lineB-segment1",
        "ok": ok,
        "checks": results,
        "counts": {"total": len(results), "passed": sum(1 for r in results if r["ok"])},
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False))
    else:
        print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}: "
              f"{receipt['counts']['passed']}/{receipt['counts']['total']} checks")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
