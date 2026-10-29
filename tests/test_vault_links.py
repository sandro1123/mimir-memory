"""Tests for vault link-graph analysis."""
from mimir_v8.vault_links import (
    build_link_graph,
    extract_links,
    find_dead_links,
    find_islands,
)


def test_extract_handles_alias_heading_embed():
    text = "[[A]] [[B|别名]] ![[C]] [[D#小节]]"
    assert extract_links(text) == ["A", "B", "C", "D"]


def test_extract_ignores_empty_targets():
    assert extract_links("[[]] [[   ]] [[ok]]") == ["ok"]


def test_graph_resolves_by_basename():
    notes = {"a/Home.md": "[[B]]", "b/B.md": "[[Home]]", "c/C.md": "no links"}
    graph = build_link_graph(notes)
    assert graph["a/Home.md"] == {"b/B.md"}
    assert graph["b/B.md"] == {"a/Home.md"}


def test_graph_resolves_by_relpath():
    notes = {"a/Home.md": "[[b/B]]", "b/B.md": ""}
    graph = build_link_graph(notes)
    assert graph["a/Home.md"] == {"b/B.md"}


def test_dead_links_are_reported():
    notes = {"a/Home.md": "[[Missing]]", "b/B.md": "[[AlsoMissing]]"}
    graph = build_link_graph(notes)
    dead = find_dead_links(graph, notes)
    assert ("a/Home.md", "Missing") in dead
    assert ("b/B.md", "AlsoMissing") in dead


def test_islands_have_no_links_either_way():
    notes = {"a/A.md": "[[B]]", "b/B.md": "", "c/C.md": "alone"}
    islands = find_islands(build_link_graph(notes))
    assert islands == ["c/C.md"]


def test_inbound_links_prevent_island():
    notes = {"hub/Home.md": "[[Leaf]]", "x/Leaf.md": "no outbound"}
    islands = find_islands(build_link_graph(notes))
    assert islands == []


def test_md_suffix_is_stripped():
    notes = {"a/A.md": "[[B.md]]", "b/B.md": ""}
    graph = build_link_graph(notes)
    assert graph["a/A.md"] == {"b/B.md"}
