"""Unit tests for Dynamic Agent and Domain Registry (v12.1.0)."""

from __future__ import annotations

import pytest
from mimir_v8.schema import (
    AGENT_IDS,
    DOMAINS,
    get_registered_agents,
    get_registered_domains,
    register_agent,
    register_domain,
    validate_agent_id,
    validate_domain,
    SchemaValidationError,
)


def test_default_registered_agents():
    agents = get_registered_agents()
    assert "mentor" in agents
    assert "heimdallr" in agents
    assert "jarvis" in agents
    assert "quantmaster" in agents


def test_default_registered_domains():
    domains = get_registered_domains()
    assert "system" in domains
    assert "quant" in domains
    assert "infrastructure" in domains
    assert "knowledge" in domains


def test_dynamic_agent_registration():
    # Register a new agent
    register_agent("new_analyst_agent")
    agents = get_registered_agents()
    assert "new_analyst_agent" in agents
    
    # Should validate without error
    validate_agent_id("new_analyst_agent")


def test_dynamic_domain_registration():
    # Register a new domain
    register_domain("crypto_defi")
    domains = get_registered_domains()
    assert "crypto_defi" in domains
    
    # Should validate without error
    validate_domain("crypto_defi")


def test_invalid_agent_validation():
    with pytest.raises(SchemaValidationError):
        validate_agent_id("completely_unregistered_agent_xyz_123")


def test_invalid_domain_validation():
    with pytest.raises(SchemaValidationError):
        validate_domain("unregistered_domain_xyz_123")
