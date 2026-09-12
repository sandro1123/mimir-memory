#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.0-B2 笔记本端联邦节点 — Tailscale 真双节点实测（笔记本侧）。

角色 node_id="laptop"。独立一次性库 ~/fed_test/laptop.db。
信封经真网络（scp over Tailscale）与 n100 交换——协议层之外的传输
层即「真实双节点」判据（无内存共享、无同机文件系统捷径）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\mimir-work\mimir")
from mimir_v8.federation import FederationService, encrypt_envelope
from mimir_v8.store import CanonicalStore

ROOT = Path(r"C:\mimir-work\fed_test")
ROOT.mkdir(exist_ok=True)
MODE = sys.argv[1] if len(sys.argv) > 1 else ""

svc = FederationService(CanonicalStore(ROOT / "laptop.db"), node_id="laptop")

if MODE == "pubkey":
    print(json.dumps({"node_id": "laptop", "public_key": svc.public_key}))

elif MODE == "register-n100":
    key = json.loads((ROOT / "n100.pubkey.json").read_text(encoding="utf-8"))["public_key"]
    r = svc.register_peer("n100", key)
    print(json.dumps(r))

elif MODE == "publish":
    svc.append_event({"key": "shared/pref/theme", "lamport": 2,
                      "node_id": "laptop", "op": "set", "value": "dark"})
    svc.append_event({"key": "shared/skill/fed-deploy", "lamport": 4,
                      "node_id": "laptop", "op": "set", "value": "tailscale-runbook-v1"})
    print(json.dumps({"published": 2}))

elif MODE == "export-to-n100":
    env = svc.export_events(since=0, to_peer="n100")
    (ROOT / "laptop_to_n100.envelope.json").write_text(
        json.dumps(env, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"written": "laptop_to_n100.envelope.json", "count": env["count"]}))

elif MODE == "ingest-from-n100":
    env = json.loads((ROOT / "n100_to_laptop.envelope.json").read_text(encoding="utf-8"))
    r = svc.ingest_envelope(env)
    print(json.dumps(r))

elif MODE == "state":
    out = {}
    for key in ("shared/pref/theme", "shared/skill/fed-deploy",
                "shared/skill/k8s-drain"):
        st = svc.crdt_state(key)
        out[key] = {"value": st["value"] if st else None,
                    "lamport": st["lamport"] if st else None,
                    "op": st["op"] if st else None,
                    "winner_node": st["node_id"] if st else None}
    print(json.dumps(out))

elif MODE == "purge":
    for f in ROOT.glob("*.db"):
        f.unlink()
    print(json.dumps({"purged": True}))
