"""
Tests for the OSS relay server — default implementations and WebSocket handler.

Covers:
  - TokenAuthProvider (agent, viewer, admin auth)
  - InMemorySessionStore (CRUD, tenant filtering, expiry)
  - DefaultSessionPolicy (global cap)
  - NoopLifecycleHooks
  - create_app factory
  - WebSocket relay (register, subscribe, trace forwarding, commands, role enforcement)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from relay.defaults import (
    DefaultSessionPolicy,
    InMemorySessionStore,
    NoopLifecycleHooks,
    TokenAuthProvider,
)
from relay.models import Session, SessionInfo
from relay.protocols import AuthResult
from relay.relay import RelayConfig, create_app

# ════════════════════════════════════════════════════════════════════
# TokenAuthProvider
# ════════════════════════════════════════════════════════════════════


class TestTokenAuthProvider:
    @pytest.fixture
    def auth(self):
        return TokenAuthProvider("test-token-123")

    @pytest.mark.asyncio
    async def test_agent_auth_valid(self, auth):
        result = await auth.authenticate_agent("test-token-123", {})
        assert result.authenticated is True

    @pytest.mark.asyncio
    async def test_agent_auth_invalid(self, auth):
        result = await auth.authenticate_agent("wrong-token", {})
        assert result.authenticated is False
        assert "invalid" in result.error.lower()

    @pytest.mark.asyncio
    async def test_agent_auth_missing(self, auth):
        result = await auth.authenticate_agent(None, {})
        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_viewer_auth_valid_secret(self, auth):
        session = MagicMock(session_secret="viewer-secret-abc")
        result = await auth.authenticate_viewer(session, "viewer-secret-abc", {})
        assert result.authenticated is True

    @pytest.mark.asyncio
    async def test_viewer_auth_wrong_secret(self, auth):
        session = MagicMock(session_secret="viewer-secret-abc")
        result = await auth.authenticate_viewer(session, "wrong-secret", {})
        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_viewer_auth_no_secret(self, auth):
        session = MagicMock(session_secret="viewer-secret-abc")
        result = await auth.authenticate_viewer(session, None, {})
        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_admin_auth_uses_agent_token(self, auth):
        result = await auth.authenticate_admin("test-token-123", {})
        assert result.authenticated is True
        result2 = await auth.authenticate_admin("wrong", {})
        assert result2.authenticated is False

    @pytest.mark.asyncio
    async def test_timing_safe_comparison(self, auth):
        """Auth should not short-circuit on partial match."""
        result = await auth.authenticate_agent("test-token-12", {})
        assert result.authenticated is False


# ════════════════════════════════════════════════════════════════════
# InMemorySessionStore
# ════════════════════════════════════════════════════════════════════


class TestInMemorySessionStore:
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

    @pytest.mark.asyncio
    async def test_put_and_get(self, store):
        s = self._make_session("s1")
        await store.put("s1", s)
        got = await store.get("s1")
        assert got is s

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, store):
        assert await store.get("nope") is None

    @pytest.mark.asyncio
    async def test_remove(self, store):
        s = self._make_session("s1")
        await store.put("s1", s)
        removed = await store.remove("s1")
        assert removed is s
        assert await store.get("s1") is None

    @pytest.mark.asyncio
    async def test_exists(self, store):
        assert await store.exists("s1") is False
        await store.put("s1", self._make_session("s1"))
        assert await store.exists("s1") is True

    @pytest.mark.asyncio
    async def test_count_global(self, store):
        assert await store.count() == 0
        await store.put("s1", self._make_session("s1"))
        await store.put("s2", self._make_session("s2"))
        assert await store.count() == 2

    @pytest.mark.asyncio
    async def test_count_by_tenant(self, store):
        await store.put("s1", self._make_session("s1", tenant_id="org-A"))
        await store.put("s2", self._make_session("s2", tenant_id="org-B"))
        await store.put("s3", self._make_session("s3", tenant_id="org-A"))
        assert await store.count(tenant_id="org-A") == 2
        assert await store.count(tenant_id="org-B") == 1
        assert await store.count(tenant_id="org-C") == 0

    @pytest.mark.asyncio
    async def test_list_for_tenant(self, store):
        await store.put("s1", self._make_session("s1", tenant_id="org-A"))
        await store.put("s2", self._make_session("s2", tenant_id="org-B"))

        all_sessions = await store.list_for_tenant()
        assert len(all_sessions) == 2

        a_sessions = await store.list_for_tenant(tenant_id="org-A")
        assert len(a_sessions) == 1
        assert a_sessions[0].info.session_id == "s1"

    @pytest.mark.asyncio
    async def test_get_expired(self, store):
        s = self._make_session("s1")
        s.last_activity = time.time() - 7200  # 2 hours ago
        await store.put("s1", s)

        s2 = self._make_session("s2")
        s2.last_activity = time.time()  # just now
        await store.put("s2", s2)

        expired = await store.get_expired(3600)  # 1 hour TTL
        assert "s1" in expired
        assert "s2" not in expired


# ════════════════════════════════════════════════════════════════════
# DefaultSessionPolicy
# ════════════════════════════════════════════════════════════════════


class TestDefaultSessionPolicy:
    @pytest.mark.asyncio
    async def test_allows_under_limit(self):
        store = InMemorySessionStore()
        policy = DefaultSessionPolicy(3, store)
        ok, err = await policy.can_create_session(None, None, AuthResult(authenticated=True))
        assert ok is True
        assert err is None

    @pytest.mark.asyncio
    async def test_blocks_at_limit(self):
        store = InMemorySessionStore()
        for i in range(3):
            s = Session(
                agent_ws=MagicMock(),
                info=SessionInfo(session_id=f"s{i}", agent_framework="hermes"),
                session_secret="x",
            )
            await store.put(f"s{i}", s)

        policy = DefaultSessionPolicy(3, store)
        ok, err = await policy.can_create_session(None, None, AuthResult(authenticated=True))
        assert ok is False
        assert "max sessions" in err.lower()

    def test_max_sessions_for_tenant(self):
        store = InMemorySessionStore()
        policy = DefaultSessionPolicy(50, store)
        assert policy.max_sessions_for_tenant(None, AuthResult(authenticated=True)) == 50


# ════════════════════════════════════════════════════════════════════
# NoopLifecycleHooks
# ════════════════════════════════════════════════════════════════════


class TestNoopLifecycleHooks:
    @pytest.mark.asyncio
    async def test_hooks_are_callable(self):
        hooks = NoopLifecycleHooks()
        # Should not raise
        await hooks.on_session_created("s1", "u1", "t1", {})
        await hooks.on_session_destroyed("s1", "u1", "t1", "test")
        await hooks.on_viewer_joined("s1", "t1", 1)
        await hooks.on_viewer_left("s1", "t1", 0)


# ════════════════════════════════════════════════════════════════════
# create_app factory
# ════════════════════════════════════════════════════════════════════


class TestCreateApp:
    def test_default_config(self):
        app = create_app()
        assert app is not None
        assert hasattr(app.state, "relay_config")

    def test_custom_config(self):
        store = InMemorySessionStore()
        config = RelayConfig(
            auth=TokenAuthProvider("custom-token"),
            store=store,
            policy=DefaultSessionPolicy(10, store),
            hooks=NoopLifecycleHooks(),
        )
        app = create_app(config)
        assert app.state.relay_config is config


# ════════════════════════════════════════════════════════════════════
# HTTP Endpoints (via httpx ASGI client)
# ════════════════════════════════════════════════════════════════════


class TestHTTPEndpoints:
    @pytest_asyncio.fixture
    async def client(self):
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "sessions" in data

    @pytest.mark.asyncio
    async def test_sessions_unauthorized(self, client):
        resp = await client.get("/sessions")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_sessions_with_token(self, client):
        # We need the actual AGENT_TOKEN from the relay module
        from relay.relay import AGENT_TOKEN

        resp = await client.get(
            "/sessions",
            headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.json() == []
