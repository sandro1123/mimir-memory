#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.0-B2 n100 端联邦节点 — Tailscale 真双节点实测（设备侧）。

角色 node_id="n100"。独立库 ~/fed_test/n100.db（与生产库完全隔离）。
信封经 scp over Tailscale 与笔记本交换。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mimir_v8.federation import FederationService
from mimir_v8.store import CanonicalStore

ROOT = Path.home() / "fed_test"
ROOT.mkdir(exist_ok=True)
MODE = sys.argv[1] if len(sys.argv) > 1 else ""

svc = FederationService(CanonicalStore(ROOT / "n100.db"), node_id="n100")

if MODE == "pubkey":
    print(json.dumps({"node_id": "n100", "public_key": svc.public_key}))

elif MODE == "register-laptop":
    key = json.loads((ROOT / "laptop.pubkey.json").read_text(encoding="utf-8"))["public_key"]
    r = svc.register_peer("laptop", key)
    print(json.dumps(r))

elif MODE == "publish":
    svc.append_event({"key": "shared/skill/k8s-drain", "lamport": 3,
                      "node_id": "n100", "op": "set", "value": "runbook-v1"})
    svc.append_event({"key": "shared/skill/k8s-drain", "lamport": 4,
                      "node_id": "n100", "op": "set", "value": "runbook-v2"})
    svc.append_event({"key": "shared/pref/theme", "lamport": 4,
                      "node_id": "n100", "op": "set", "value": "light"})
    print(json.dumps({"published": 3}))

elif MODE == "export-to-laptop":
    env = svc.export_events(since=0, to_peer="laptop")
    (ROOT / "n100_to_laptop.envelope.json").write_text(
        json.dumps(env, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"written": "n100_to_laptop.envelope.json", "count": env["count"]}))

elif MODE == "ingest-from-laptop":
    env = json.loads((ROOT / "laptop_to_n100.envelope.json").read_text(encoding="utf-8"))
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
