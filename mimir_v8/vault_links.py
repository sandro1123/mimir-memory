"""Link-graph analysis for an Obsidian vault: dead links and islands.

Resolution rule (stated here so "dead links == 0" is falsifiable):

    A ``[[target]]`` resolves when either

    1. ``target`` equals a note's vault-relative path without the ``.md``
       suffix, or
    2. ``target`` (basename only) equals exactly one note's basename.
       When several notes share a basename, the link resolves to all of
       them.

    Anchors (``#heading``) and the ``.md`` suffix are stripped before
    matching. Matching is case-sensitive, as Obsidian's is on Linux.

This is approximates Obsidian's own vault-wide basename resolution; it
deliberately ignores aliases declared in frontmatter, so a link that
Obsidian resolves via an alias is reported here as dead. That is the
conservative direction: the report over-counts rather than under-counts.
"""

from __future__ import annotations

import re

#: Matches ``[[target]]``, ``[[target|alias]]``, ``![[target]]`` and
#: ``[[target#heading]]``; captures the raw target before ``|`` or ``#``.
LINK_RE = re.compile(r"!?\[\[([^\]|#]*)")


def extract_links(text: str) -> list[str]:
    """Return the raw link targets in ``text``, in document order."""
    targets: list[str] = []
    for match in LINK_RE.finditer(text):
        target = match.group(1).strip()
        if target:
            targets.append(target)
    return targets


def _normalise(target: str) -> str:
    """Strip a trailing ``.md`` and any surrounding whitespace."""
    cleaned = target.strip().replace("\\", "/")
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    return cleaned


def _index_notes(notes: dict[str, str]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Build (relpath-without-ext -> relpath) and (basename -> relpaths)."""
    by_relpath: dict[str, str] = {}
    by_basename: dict[str, set[str]] = {}
    for relpath in notes:
        without_ext = relpath[:-3] if relpath.lower().endswith(".md") else relpath
        by_relpath[without_ext] = relpath
        basename = without_ext.rsplit("/", 1)[-1]
        by_basename.setdefault(basename, set()).add(relpath)
    return by_relpath, by_basename


def _resolve(target: str, by_relpath: dict[str, str],
             by_basename: dict[str, set[str]]) -> set[str]:
    """Resolve one link target to the set of note relpaths it points at."""
    normalised = _normalise(target)
    if normalised in by_relpath:
        return {by_relpath[normalised]}
    basename = normalised.rsplit("/", 1)[-1]
    return set(by_basename.get(basename, ()))


def build_link_graph(notes: dict[str, str]) -> dict[str, set[str]]:
    """Map each note relpath to the relpaths it links to."""
    by_relpath, by_basename = _index_notes(notes)
    graph: dict[str, set[str]] = {}
    for relpath, text in notes.items():
        resolved: set[str] = set()
        for target in extract_links(text):
            resolved |= _resolve(target, by_relpath, by_basename)
        graph[relpath] = resolved
    return graph


def find_dead_links(
    graph: dict[str, set[str]], notes: dict[str, str]
) -> list[tuple[str, str]]:
    """Return ``(source_relpath, raw_target)`` for every unresolvable link."""
    by_relpath, by_basename = _index_notes(notes)
    dead: list[tuple[str, str]] = []
    for relpath, text in notes.items():
        for target in extract_links(text):
            if not _resolve(target, by_relpath, by_basename):
                dead.append((relpath, target))
    return dead


def find_islands(graph: dict[str, set[str]]) -> list[str]:
    """Return notes with no outbound links and no inbound links."""
    has_inbound: set[str] = set()
    for targets in graph.values():
        has_inbound |= targets
    return sorted(
        relpath
        for relpath, targets in graph.items()
        if not targets and relpath not in has_inbound
    )
