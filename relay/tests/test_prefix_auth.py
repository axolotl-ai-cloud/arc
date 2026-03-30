"""Tests for prefix-based token authentication (beta mode)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from relay.defaults import TokenAuthProvider
from relay.models import Session, SessionInfo


@pytest.fixture
def beta_provider():
    """Provider with prefix-based auth enabled."""
    with patch.dict(os.environ, {"AGENT_TOKEN_PREFIX": "axolotl_beta_"}):
        # Re-import to pick up env var
        import importlib

        import relay.defaults as defaults_mod

        importlib.reload(defaults_mod)
        yield defaults_mod.TokenAuthProvider(agent_token="")
    # Restore
    import importlib

    import relay.defaults as defaults_mod

    importlib.reload(defaults_mod)


@pytest.fixture
def fixed_provider():
    """Provider with a fixed token (OSS mode)."""
    return TokenAuthProvider(agent_token="my-fixed-token")


@pytest.fixture
def sample_session():
    """A session with a known secret."""
    return Session(
        agent_ws=None,
        info=SessionInfo(
            agent_framework="hermes",
            session_id="test-session",
            started_at="2024-01-01T00:00:00Z",
        ),
        session_secret="viewer-secret-123",
        user_id=None,
        tenant_id=None,
    )


# ─── Fixed Token Tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fixed_token_accepts_correct(fixed_provider):
    result = await fixed_provider.authenticate_agent("my-fixed-token", {})
    assert result.authenticated is True


@pytest.mark.asyncio
async def test_fixed_token_rejects_wrong(fixed_provider):
    result = await fixed_provider.authenticate_agent("wrong-token", {})
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_fixed_token_rejects_empty(fixed_provider):
    result = await fixed_provider.authenticate_agent(None, {})
    assert result.authenticated is False


# ─── Prefix Token Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_prefix_accepts_valid_token(beta_provider):
    # 43+ chars after prefix
    token = "axolotl_beta_" + "A" * 43
    result = await beta_provider.authenticate_agent(token, {})
    assert result.authenticated is True
    assert result.user_id is not None  # Should get a user_id for isolation


@pytest.mark.asyncio
async def test_prefix_rejects_short_hash(beta_provider):
    # Only 10 chars after prefix — too short
    token = "axolotl_beta_" + "A" * 10
    result = await beta_provider.authenticate_agent(token, {})
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_prefix_rejects_wrong_prefix(beta_provider):
    token = "wrong_prefix_" + "A" * 43
    result = await beta_provider.authenticate_agent(token, {})
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_prefix_rejects_invalid_chars(beta_provider):
    # Spaces and special chars should be rejected
    token = "axolotl_beta_" + "A" * 40 + " ! "
    result = await beta_provider.authenticate_agent(token, {})
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_prefix_different_tokens_get_different_user_ids(beta_provider):
    token_a = "axolotl_beta_" + "A" * 43
    token_b = "axolotl_beta_" + "B" * 43
    result_a = await beta_provider.authenticate_agent(token_a, {})
    result_b = await beta_provider.authenticate_agent(token_b, {})
    assert result_a.user_id != result_b.user_id


@pytest.mark.asyncio
async def test_prefix_same_token_gets_same_user_id(beta_provider):
    token = "axolotl_beta_" + "C" * 43
    result_1 = await beta_provider.authenticate_agent(token, {})
    result_2 = await beta_provider.authenticate_agent(token, {})
    assert result_1.user_id == result_2.user_id


# ─── Viewer Auth Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_viewer_accepts_correct_secret(fixed_provider, sample_session):
    result = await fixed_provider.authenticate_viewer(sample_session, "viewer-secret-123", {})
    assert result.authenticated is True


@pytest.mark.asyncio
async def test_viewer_rejects_wrong_secret(fixed_provider, sample_session):
    result = await fixed_provider.authenticate_viewer(sample_session, "wrong-secret", {})
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_viewer_rejects_empty_secret(fixed_provider, sample_session):
    result = await fixed_provider.authenticate_viewer(sample_session, None, {})
    assert result.authenticated is False
