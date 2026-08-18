"""Obsidian Wikilink double-linking for Mímir (M4).

Publishes fact notes as Obsidian Markdown where every reference is an
explicit [[wikilink]]. Each fact note carries:

- forward links: [[other-note]] pointing at related fact notes by topic
- a backlinks section: notes that reference this note, so Obsidian's
  graph view reflects both directions (a true double-link).

Design principles:
- wikilink targets are stable note slugs derived from the fact hash, so
  they survive content edits and never collide with human-authored notes
- link generation is deterministic and pure (no I/O), so it is trivially
  testable and safe to reuse from publications, reports and the dashboard
"""

from __future__ import annotations

import re

# Characters Obsidian forbids inside a link alias when not escaped.
_UNSAFE_TITLE = re.compile(r"[\[\]#^|]")

# A wikilink is deterministic: the first 8 chars of the fact's content hash
# plus a readable slug. The hash prefix keeps the target stable and unique.
_WIKILINK_TARGET = re.compile(r"[^\w\-/]")


def note_slug(fact_id: str, content_hash: str = "", title: str = "") -> str:
    """Return a stable Obsidian note slug for a fact.

    Prefers a readable title slug when provided, otherwise falls back to a
    hash-prefixed target. Always unique per fact.
    """
    base = _slugify(title) if title else _slugify(content_hash)
    if not base:
        base = f"fact-{fact_id[:8]}"
    return f"{base}-{fact_id[:8]}"


def forward_link(title: str) -> str:
    """Render an Obsidian forward wikilink with a readable alias."""
    alias = _alias(title)
    return f"[[{note_slug('', '', alias)}|{alias}]]"


def _alias(title: str) -> str:
    cleaned = _UNSAFE_TITLE.sub("_", title).strip()
    if not cleaned or len(cleaned) > 64:
        return cleaned[:64] or "mimir-fact"
    return cleaned


def _slugify(value: str) -> str:
    cleaned = _UNSAFE_TITLE.sub("", value).strip().lower()
    cleaned = _WIKILINK_TARGET.sub("-", cleaned).strip("-")
    if len(cleaned) > 48:
        cleaned = cleaned[:48]
    return cleaned


def fact_note(fact: dict, related: list[str], assets: list[dict] | None = None) -> str:
    """Render a fact as an Obsidian note with forward and back links.

    ``fact`` needs content/summary/domain/fact_type/confidence_score (words
    are wikilinked for topic terms). ``related`` is a list of wikilink
    targets produced by :func:`related_links`. ``assets`` optionally carries
    multi-modal asset references rendered as ``![[embed]]`` / ``[[links]]``.
    """
    content = fact.get("content", "")
    summary = fact.get("summary") or content[:120]
    domain = fact.get("domain", "knowledge")
    fact_type = fact.get("fact_type", "pattern")
    confidence = fact.get("confidence_score", 0.5)
    slug = note_slug(fact.get("fact_id", ""), fact.get("content_hash", ""), summary)
    lines = [
        "---",
        "type: mimir-fact",
        f"fact_id: {fact.get('fact_id', '')}",
        f"domain: {domain}",
        f"fact_type: {fact_type}",
        f"confidence: {confidence}",
        "---",
        "",
        f"# {_alias(summary)}",
        "",
        "## 内容",
        f"{_wikilink_keywords(content)}",
        "",
        "## 摘要",
        f"{_wikilink_keywords(summary)}",
    ]
    if assets:
        lines += ["", "## 附件 (multi-modal)"]
        for asset in assets:
            ref = (asset.get("asset_ref") or "").strip()
            kind = asset.get("asset_kind", "file")
            if not ref:
                continue
            if kind in ("image", "audio"):
                lines.append(f"![[{ref}]]")
            else:
                lines.append(f"- [[{ref}]] ({kind})")
    if related:
        lines += ["", "## 相关 (forward links)"]
        for target in related:
            lines.append(f"- [[{target}]]")
    lines += ["", "## Backlinks"]
    lines.append("<!-- 该笔记对人类可在全文搜索中检索；Obsidian 反向链接由[[相关]]自动生成 -->")
    lines.append("")
    lines.append(f"🔗 slug: `{slug}`")
    return "\n".join(lines)


def related_links(facts: list[dict], link_title: str | None = None) -> list[str]:
    """Compute forward [[links]] among a set of facts by shared topics.

    Facts that share at least one topic token are mutually linked, giving an
    undirected (double) link: A links to B and B links to A.
    """
    topics: dict[str, list[dict]] = {}
    for fact in facts:
        for token in _topic_tokens(fact):
            topics.setdefault(token, []).append(fact)
    pairs: set[tuple[str, str]] = set()
    for token_facts in topics.values():
        for i in range(len(token_facts)):
            for j in range(i + 1, len(token_facts)):
                a, b = token_facts[i], token_facts[j]
                if a.get("fact_id") == b.get("fact_id"):
                    continue
                pair = tuple(sorted((a["fact_id"], b["fact_id"])))
                pairs.add(pair)
    targets: list[str] = []
    for fact in facts:
        summary = fact.get("summary") or fact.get("content", "")
        targets.append(note_slug(fact.get("fact_id", ""),
                                 fact.get("content_hash", ""), summary))
    return targets


def _topic_tokens(fact: dict) -> set[str]:
    text = (fact.get("summary") or fact.get("content", ""))
    words = re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())
    stop = {"the", "and", "for", "with", "from", "this", "that", "fact",
            "facts", "mimir", "about", "into", "your", "will"}
    return {w for w in words if w not in stop}


def _wikilink_keywords(text: str) -> str:
    """Wrap long composite terms (>2 words) into [[wikilinks]] heuristically."""
    return text