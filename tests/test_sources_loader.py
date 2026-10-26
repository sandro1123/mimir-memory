"""Tests for the v1.6.0 sources.yaml loader (second step: probe & source registry)."""
import tempfile
from pathlib import Path

from mimir_v8.store import load_sources_yaml


def test_load_sources_yaml_default_path():
    """Default package sources.yaml loads and has the three normalized sections."""
    data = load_sources_yaml()
    assert isinstance(data, dict)
    # The three declaration sections are always present as lists.
    assert isinstance(data["models"], list)
    assert isinstance(data["github"], list)
    assert isinstance(data["tools"], list)
    # The shipped config declares at least the example models + github + tools.
    assert len(data["models"]) >= 1
    assert len(data["github"]) >= 1
    assert len(data["tools"]) >= 1


def test_load_sources_yaml_from_custom_path():
    """A custom path is honored and parsed the same way."""
    payload = (
        "models:\n"
        "  - name: \"M1\"\n"
        "    kind: \"free_model_pool\"\n"
        "    endpoint: \"https://x.example/m\"\n"
        "github: []\n"
        "tools: []\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sources.yaml"
        p.write_text(payload, encoding="utf-8")
        data = load_sources_yaml(p)
    assert data["models"][0]["name"] == "M1"
    assert data["github"] == []
    assert data["tools"] == []


def test_load_sources_yaml_missing_file_raises():
    """A non-existent path must raise, not silently return empty config."""
    try:
        load_sources_yaml("/nonexistent/path/sources.yaml")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_load_sources_yaml_bad_top_level_raises():
    """A non-mapping top level must raise a clear error."""
    payload = "- just\n- a\n- list\n"
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sources.yaml"
        p.write_text(payload, encoding="utf-8")
        try:
            load_sources_yaml(p)
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_load_sources_yaml_bad_section_type_raises():
    """A non-list section must raise a clear error."""
    payload = "models: not-a-list\ngithub: []\ntools: []\n"
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sources.yaml"
        p.write_text(payload, encoding="utf-8")
        try:
            load_sources_yaml(p)
            assert False, "expected ValueError"
        except ValueError:
            pass
