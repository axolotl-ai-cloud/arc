"""
API Contract Enforcement Tests

These tests verify that the protocol interfaces defined in relay.protocols
maintain their expected signatures and that both the OSS defaults and hosted
implementations conform to the contracts.

When the repo is split, these tests should run against the OSS relay package
to ensure the hosted side can still extend it safely.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import fields
from unittest.mock import MagicMock

import pytest

from relay.defaults import (
    DefaultSessionPolicy,
    InMemorySessionStore,
    NoopLifecycleHooks,
    TokenAuthProvider,
)
from relay.models import Session, SessionInfo
from relay.protocols import (
    AuthProvider,
    AuthResult,
    LifecycleHooks,
    SessionPolicy,
    SessionStore,
)
from relay.relay import RelayConfig, create_app

# ════════════════════════════════════════════════════════════════════
# Contract: AuthResult dataclass shape
# ════════════════════════════════════════════════════════════════════


class TestAuthResultContract:
    """Verify AuthResult has the expected fields and defaults."""

    def test_required_fields(self):
        field_names = {f.name for f in fields(AuthResult)}
        assert "authenticated" in field_names
        assert "user_id" in field_names
        assert "tenant_id" in field_names
        assert "error" in field_names
        assert "metadata" in field_names

    def test_defaults(self):
        result = AuthResult(authenticated=True)
        assert result.user_id is None
        assert result.tenant_id is None
        assert result.error is None
        assert result.metadata == {}

    def test_full_construction(self):
        result = AuthResult(
            authenticated=True,
            user_id="user-1",
            tenant_id="org-1",
            error=None,
            metadata={"plan": "developer"},
        )
        assert result.authenticated is True
        assert result.metadata["plan"] == "developer"


# ════════════════════════════════════════════════════════════════════
# Contract: Session and SessionInfo dataclass shape
# ════════════════════════════════════════════════════════════════════


class TestSessionModelsContract:
    def test_session_info_fields(self):
        field_names = {f.name for f in fields(SessionInfo)}
        expected = {"session_id", "agent_framework", "agent_name", "started_at", "metadata"}
        assert expected.issubset(field_names)

    def test_session_info_to_dict(self):
        info = SessionInfo(session_id="s1", agent_framework="hermes", agent_name="test")
        d = info.to_dict()
        assert d["sessionId"] == "s1"
        assert d["agentFramework"] == "hermes"
        assert d["agentName"] == "test"
        assert "startedAt" in d

    def test_session_fields(self):
        field_names = {f.name for f in fields(Session)}
        expected = {
            "agent_ws",
            "info",
            "session_secret",
            "user_id",
            "tenant_id",
            "created_at",
            "last_activity",
            "viewers",
            "traces",
        }
        assert expected.issubset(field_names)

    def test_session_defaults(self):
        s = Session(
            agent_ws=MagicMock(),
            info=SessionInfo(session_id="s1", agent_framework="hermes"),
            session_secret="secret",
        )
        assert s.user_id is None
        assert s.tenant_id is None
        assert isinstance(s.viewers, set)
        assert isinstance(s.traces, list)
        assert s.created_at > 0
        assert s.last_activity > 0


# ════════════════════════════════════════════════════════════════════
# Contract: AuthProvider interface methods
# ════════════════════════════════════════════════════════════════════


class TestAuthProviderContract:
    """Verify AuthProvider protocol defines the required methods with correct signatures."""

    def test_has_authenticate_agent(self):
        assert hasattr(AuthProvider, "authenticate_agent")
        sig = inspect.signature(AuthProvider.authenticate_agent)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "token" in params
        assert "headers" in params

    def test_has_authenticate_viewer(self):
        assert hasattr(AuthProvider, "authenticate_viewer")
        sig = inspect.signature(AuthProvider.authenticate_viewer)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "session" in params
        assert "secret" in params
        assert "headers" in params

    def test_has_authenticate_admin(self):
        assert hasattr(AuthProvider, "authenticate_admin")
        sig = inspect.signature(AuthProvider.authenticate_admin)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "token" in params
        assert "headers" in params

    def test_is_runtime_checkable(self):
        """AuthProvider must be runtime_checkable for isinstance checks."""
        assert isinstance(TokenAuthProvider("t"), AuthProvider)


# ════════════════════════════════════════════════════════════════════
# Contract: SessionStore interface methods
# ════════════════════════════════════════════════════════════════════


class TestSessionStoreContract:
    def test_has_get(self):
        sig = inspect.signature(SessionStore.get)
        assert "session_id" in sig.parameters

    def test_has_put(self):
        sig = inspect.signature(SessionStore.put)
        assert "session_id" in sig.parameters
        assert "session" in sig.parameters

    def test_has_remove(self):
        sig = inspect.signature(SessionStore.remove)
        assert "session_id" in sig.parameters

    def test_has_exists(self):
        sig = inspect.signature(SessionStore.exists)
        assert "session_id" in sig.parameters

    def test_has_count_with_tenant_param(self):
        sig = inspect.signature(SessionStore.count)
        assert "tenant_id" in sig.parameters
        # tenant_id must have a default of None
        assert sig.parameters["tenant_id"].default is None

    def test_has_list_for_tenant_with_tenant_param(self):
        sig = inspect.signature(SessionStore.list_for_tenant)
        assert "tenant_id" in sig.parameters
        assert sig.parameters["tenant_id"].default is None

    def test_has_get_expired(self):
        sig = inspect.signature(SessionStore.get_expired)
        assert "ttl_seconds" in sig.parameters

    def test_is_runtime_checkable(self):
        assert isinstance(InMemorySessionStore(), SessionStore)


# ════════════════════════════════════════════════════════════════════
# Contract: SessionPolicy interface methods
# ════════════════════════════════════════════════════════════════════


class TestSessionPolicyContract:
    def test_has_can_create_session(self):
        sig = inspect.signature(SessionPolicy.can_create_session)
        params = list(sig.parameters.keys())
        assert "user_id" in params
        assert "tenant_id" in params
        assert "auth_result" in params

    def test_has_max_sessions_for_tenant(self):
        sig = inspect.signature(SessionPolicy.max_sessions_for_tenant)
        params = list(sig.parameters.keys())
        assert "tenant_id" in params
        assert "auth_result" in params

    def test_is_runtime_checkable(self):
        store = InMemorySessionStore()
        assert isinstance(DefaultSessionPolicy(10, store), SessionPolicy)


# ════════════════════════════════════════════════════════════════════
# Contract: LifecycleHooks interface methods
# ════════════════════════════════════════════════════════════════════


class TestLifecycleHooksContract:
    def test_has_on_session_created(self):
        sig = inspect.signature(LifecycleHooks.on_session_created)
        params = list(sig.parameters.keys())
        assert "session_id" in params
        assert "user_id" in params
        assert "tenant_id" in params
        assert "metadata" in params

    def test_has_on_session_destroyed(self):
        sig = inspect.signature(LifecycleHooks.on_session_destroyed)
        params = list(sig.parameters.keys())
        assert "session_id" in params
        assert "user_id" in params
        assert "tenant_id" in params
        assert "reason" in params

    def test_has_on_viewer_joined(self):
        sig = inspect.signature(LifecycleHooks.on_viewer_joined)
        params = list(sig.parameters.keys())
        assert "session_id" in params
        assert "tenant_id" in params
        assert "viewer_count" in params

    def test_has_on_viewer_left(self):
        sig = inspect.signature(LifecycleHooks.on_viewer_left)
        params = list(sig.parameters.keys())
        assert "session_id" in params
        assert "tenant_id" in params
        assert "viewer_count" in params

    def test_is_runtime_checkable(self):
        assert isinstance(NoopLifecycleHooks(), LifecycleHooks)


# ════════════════════════════════════════════════════════════════════
# Contract: RelayConfig and create_app
# ════════════════════════════════════════════════════════════════════


class TestRelayConfigContract:
    def test_config_fields(self):
        field_names = {f.name for f in fields(RelayConfig)}
        assert field_names == {"auth", "store", "policy", "hooks"}

    def test_create_app_returns_fastapi(self):
        app = create_app()
        from fastapi import FastAPI

        assert isinstance(app, FastAPI)

    def test_create_app_stores_config(self):
        store = InMemorySessionStore()
        config = RelayConfig(
            auth=TokenAuthProvider("test"),
            store=store,
            policy=DefaultSessionPolicy(10, store),
            hooks=NoopLifecycleHooks(),
        )
        app = create_app(config)
        assert app.state.relay_config is config

    def test_create_app_default_has_config(self):
        app = create_app()
        assert hasattr(app.state, "relay_config")
        config = app.state.relay_config
        assert isinstance(config.auth, AuthProvider)
        assert isinstance(config.store, SessionStore)
        assert isinstance(config.policy, SessionPolicy)
        assert isinstance(config.hooks, LifecycleHooks)


# ════════════════════════════════════════════════════════════════════
# Contract: OSS defaults implement all protocol methods correctly
# ════════════════════════════════════════════════════════════════════


class TestOSSDefaultsBehavioralContract:
    """
    Verify OSS default implementations satisfy the behavioral contract.
    These are the same behaviors the hosted implementations must match.
    """

    @pytest.fixture
    def store(self):
        return InMemorySessionStore()

    def _make_session(self, sid: str, tenant_id: str | None = None) -> Session:
        return Session(
            agent_ws=MagicMock(),
            info=SessionInfo(session_id=sid, agent_framework="hermes"),
            session_secret="secret",
            tenant_id=tenant_id,
        )

    # -- Store contract behaviors --

    @pytest.mark.asyncio
    async def test_store_get_returns_none_for_missing(self, store):
        """Contract: get() returns None, not raises, for missing sessions."""
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_store_remove_returns_none_for_missing(self, store):
        """Contract: remove() returns None for missing sessions."""
        result = await store.remove("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_store_count_none_returns_global(self, store):
        """Contract: count(tenant_id=None) returns total across all tenants."""
        await store.put("s1", self._make_session("s1", "org-A"))
        await store.put("s2", self._make_session("s2", "org-B"))
        assert await store.count(tenant_id=None) == 2

    @pytest.mark.asyncio
    async def test_store_count_scoped_to_tenant(self, store):
        """Contract: count(tenant_id='X') only counts that tenant's sessions."""
        await store.put("s1", self._make_session("s1", "org-A"))
        await store.put("s2", self._make_session("s2", "org-B"))
        assert await store.count(tenant_id="org-A") == 1

    @pytest.mark.asyncio
    async def test_store_list_for_tenant_none_returns_all(self, store):
        """Contract: list_for_tenant(None) returns all sessions."""
        await store.put("s1", self._make_session("s1", "org-A"))
        await store.put("s2", self._make_session("s2", "org-B"))
        all_sessions = await store.list_for_tenant(tenant_id=None)
        assert len(all_sessions) == 2

    @pytest.mark.asyncio
    async def test_store_list_for_tenant_scoped(self, store):
        """Contract: list_for_tenant('X') only returns that tenant's sessions."""
        await store.put("s1", self._make_session("s1", "org-A"))
        await store.put("s2", self._make_session("s2", "org-B"))
        a_sessions = await store.list_for_tenant(tenant_id="org-A")
        assert len(a_sessions) == 1
        assert a_sessions[0].info.session_id == "s1"

    @pytest.mark.asyncio
    async def test_store_get_expired_returns_ids(self, store):
        """Contract: get_expired returns list of session ID strings."""
        s = self._make_session("s1")
        s.last_activity = time.time() - 7200
        await store.put("s1", s)
        expired = await store.get_expired(3600)
        assert isinstance(expired, list)
        assert "s1" in expired

    # -- Auth contract behaviors --

    @pytest.mark.asyncio
    async def test_auth_returns_authresult_not_raises(self):
        """Contract: authenticate methods return AuthResult, never raise."""
        auth = TokenAuthProvider("token")
        result = await auth.authenticate_agent("wrong", {})
        assert isinstance(result, AuthResult)
        assert result.authenticated is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_auth_success_returns_authenticated_true(self):
        """Contract: successful auth sets authenticated=True."""
        auth = TokenAuthProvider("token")
        result = await auth.authenticate_agent("token", {})
        assert result.authenticated is True

    # -- Policy contract behaviors --

    @pytest.mark.asyncio
    async def test_policy_returns_tuple(self, store):
        """Contract: can_create_session returns (bool, str|None)."""
        policy = DefaultSessionPolicy(10, store)
        result = await policy.can_create_session(None, None, AuthResult(authenticated=True))
        assert isinstance(result, tuple)
        assert len(result) == 2
        ok, _err = result
        assert isinstance(ok, bool)

    @pytest.mark.asyncio
    async def test_policy_denied_returns_reason(self, store):
        """Contract: denied creation returns (False, 'human-readable reason')."""
        for i in range(3):
            await store.put(f"s{i}", self._make_session(f"s{i}"))
        policy = DefaultSessionPolicy(3, store)
        ok, err = await policy.can_create_session(None, None, AuthResult(authenticated=True))
        assert ok is False
        assert isinstance(err, str)
        assert len(err) > 0
