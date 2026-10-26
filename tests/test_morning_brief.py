"""TDD tests for v1.6.0 Step 3: double-layer crystallization pipeline.

Layer 1: human-readable morning-brief Markdown.
Layer 2: structured entity cards auto-ingested into the canonical store.
"""
import tempfile
from pathlib import Path

from mimir_v8.store import CanonicalStore
from mimir_v8.morning_brief import (
    MorningBriefService,
    SourceItem,
)


def _store():
    root = Path(tempfile.mkdtemp())
    store = CanonicalStore(root / "canon.db")
    store.migrate_schema_21_to_22()
    return store


def _items():
    return [
        SourceItem(
            kind="model_probe",
            source_id="Nemotron-3-Ultra",
            title="Nemotron-3-Ultra 探针可达",
            body="NVIDIA 免费模型池 Nemotron-3-Ultra 探测返回 200，延迟 42ms。",
            url="https://api.example.com/v1/models",
            status="ok",
        ),
        SourceItem(
            kind="github_release",
            source_id="litellm/litellm",
            title="litellm v1.50.0 发布",
            body="LiteLLM 1.50.0 修复了代理路由超时，新增上游健康检查。",
            url="https://github.com/BerriAI/litellm/releases/tag/v1.50.0",
        ),
    ]


# ---------------------------------------------------------------------------
# Layer 1: morning-brief Markdown
# ---------------------------------------------------------------------------

def test_render_markdown_contains_sections():
    """The brief must carry a model-probe section and a release section."""
    svc = MorningBriefService(_store())
    md = svc.render_markdown(_items(), as_of="2026-10-26T07:30:00Z")

    assert "Nemotron-3-Ultra" in md
    assert "litellm" in md
    assert "2026-10-26" in md


def test_render_markdown_handles_empty():
    """An empty item list must still yield a valid (quiet) brief, not crash."""
    svc = MorningBriefService(_store())
    md = svc.render_markdown([], as_of="2026-10-26T07:30:00Z")
    assert isinstance(md, str)
    assert "2026-10-26" in md


# ---------------------------------------------------------------------------
# Layer 2: structured entity extraction
# ---------------------------------------------------------------------------

def test_extract_cards_from_probe_and_release():
    """A model probe becomes a model_card; a release becomes a tool_card."""
    svc = MorningBriefService(_store())
    cards = svc.extract_cards(_items())

    types = {c.name: c.entity_type for c in cards}
    assert types["Nemotron-3-Ultra"] == "model_card"
    assert types["litellm/litellm"] == "tool_card"


def test_extract_cards_is_deterministic():
    """Same inputs always yield the same cards (no LLM / randomness)."""
    svc = MorningBriefService(_store())
    a = svc.extract_cards(_items())
    b = svc.extract_cards(_items())
    assert [c.name for c in a] == [c.name for c in b]
    assert [c.entity_type for c in a] == [c.entity_type for c in b]


# ---------------------------------------------------------------------------
# Closed loop: publish both layers into the canonical store
# ---------------------------------------------------------------------------

def test_publish_ingests_brief_and_cards():
    """publish writes one brief fact and links each card entity to it."""
    store = _store()
    svc = MorningBriefService(store)
    result = svc.publish(_items(), actor_principal="heimdallr")

    # Layer 1: a brief fact exists.
    assert result["brief_fact_id"]
    fact = store.get_fact(result["brief_fact_id"])
    assert fact is not None
    assert fact["domain"] == "system"

    # Layer 2: two cards ingested and linked to the brief fact.
    assert len(result["cards"]) == 2
    for card in result["cards"]:
        assert card.entity_id
        assert card.fact_id
        linked = store.get_fact_entities(result["brief_fact_id"])
        assert any(e["entity_id"] == card.entity_id for e in linked)
        assert any(e["role"] == "crystallized_entity" for e in linked)


def test_publish_is_idempotent_for_same_items():
    """Re-publishing identical items must not duplicate entity rows."""
    store = _store()
    svc = MorningBriefService(store)
    first = svc.publish(_items(), actor_principal="heimdallr")
    second = svc.publish(_items(), actor_principal="heimdallr")

    # Entity count should stay at 2 (one per distinct name), not double.
    with store.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert n == 2, f"expected 2 entities, got {n}"

    # The second run reused existing entity ids for the matching cards.
    first_ids = {c.name: c.entity_id for c in first["cards"]}
    second_ids = {c.name: c.entity_id for c in second["cards"]}
    assert first_ids == second_ids
