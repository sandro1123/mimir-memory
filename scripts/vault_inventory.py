# -*- coding: utf-8 -*-
"""Emit a reproducible inventory artifact for an Obsidian vault.

Stage 1 of the Obsidian knowledge-hub work. The numbers this prints are
the acceptance evidence for role classification, so they must be
re-runnable rather than prose in a design doc.

Usage:
    python scripts/vault_inventory.py --vault-root ~/obsidian-vault/"Sandro's Vault"
    python scripts/vault_inventory.py --vault-root DIR --out PATH --today 2026-10-28

Reference-only notes (60-敏感信息/) are listed but never read: their text
is not loaded for link extraction.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.vault_inventory import scan_vault  # noqa: E402
from mimir_v8.vault_links import (  # noqa: E402
    build_link_graph,
    find_dead_links,
    find_islands,
)
from mimir_v8.vault_roles import ROLE_REFERENCE_ONLY  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--today", default=None,
                        help="date stamp for the default output name")
    args = parser.parse_args(argv)

    vault_root = Path(args.vault_root).expanduser()
    if not vault_root.exists():
        print(f"error: vault root not found: {vault_root}", file=sys.stderr)
        return 2
    if not vault_root.is_dir():
        print(f"error: vault root is not a directory: {vault_root}",
              file=sys.stderr)
        return 2

    inventory = scan_vault(vault_root)

    # Only read notes that are not reference-only. This is the whole point
    # of the role: the sensitive directory is enumerated, never opened.
    texts: dict[str, str] = {}
    for note in inventory.notes:
        if note.role == ROLE_REFERENCE_ONLY:
            continue
        try:
            texts[note.relpath] = (vault_root / note.relpath).read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            # Unreadable notes were already recorded; they simply carry no
            # links.
            continue

    graph = build_link_graph(texts)
    dead = find_dead_links(graph, texts)
    islands = find_islands(graph)

    payload = inventory.to_json()
    payload["link_graph"] = {
        "links_total": sum(len(targets) for targets in graph.values()),
        "dead_total": len(dead),
        "dead": [{"source": source, "target": target} for source, target in dead],
        "islands_total": len(islands),
        "islands": islands,
    }

    stamp = args.today or date.today().isoformat()
    out_path = (
        Path(args.out) if args.out
        else Path(__file__).resolve().parent.parent
        / "docs" / "vault-inventory" / f"{stamp}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"scanned {len(payload['notes'])} notes under {payload['vault_root']}")
    for role, count in sorted(payload["counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {count:5d}  {role}")
    print(f"links: {payload['link_graph']['links_total']}, "
          f"dead: {payload['link_graph']['dead_total']}, "
          f"islands: {payload['link_graph']['islands_total']}")
    print(f"artifact: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
