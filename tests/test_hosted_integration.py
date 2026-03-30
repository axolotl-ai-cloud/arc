"""
Integration Tests — Hosted providers against OSS relay server.

These tests verify that the hosted implementations (WorkOSAuthProvider,
RedisSessionStore, StripePlanPolicy, BillingLifecycleHooks) correctly
plug into the OSS relay's create_app() factory and work end-to-end.

When the repo is split into two, these tests serve as the integration
boundary: they import from `relay` (OSS) and `hosted.backend` (commercial)
and verify everything wires together correctly.

All external services (Redis, WorkOS, Stripe) are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from hosted.backend.auth import WorkOSAuthProvider, hash_api_key
from hosted.backend.hooks import BillingLifecycleHooks
from hosted.backend.policy import StripePlanPolicy
from hosted.backend.store import RedisSessionStore
from httpx import ASGITransport, AsyncClient

from relay.defaults import InMemorySessionStore
from relay.models import Session, SessionInfo
from relay.protocols import AuthProvider, AuthResult, LifecycleHooks, SessionPolicy, SessionStore
from relay.relay import RelayConfig, create_app

# ════════════════════════════════════════════════════════════════════
# Shared test fixtures
# ════════════════════════════════════════════════════════════════════


class FakeRedis:
    """Minimal async Redis mock for integration tests."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        self._hashes: dict[str, dict[str, str]] = {}

    async def get(self, key):
        return self._data.get(key)

    async def set(self, key, value):
        self._data[key] = value

    async def delete(self, key):
        self._data.pop(key, None)

    async def exists(self, key):
        return key in self._data

    async def sadd(self, key, *values):
        self._sets.setdefault(key, set()).update(values)

    async def srem(self, key, *values):
        s = self._sets.get(key, set())
        for v in values:
            s.discard(v)

    async def smembers(self, key):
        return self._sets.get(key, set())

    async def scard(self, key):
        return len(self._sets.get(key, set()))

    async def hset(self, key, field, value):
        self._hashes.setdefault(key, {})[field] = value

    async def hget(self, key, field):
        return self._hashes.get(key, {}).get(field)

    async def hdel(self, key, field):
        h = self._hashes.get(key, {})
        h.pop(field, None)

    async def hincrby(self, key, field, amount):
        h = self._hashes.setdefault(key, {})
        h[field] = str(int(h.get(field, "0")) + amount)

    async def hgetall(self, key):
        return self._hashes.get(key, {})


class FakeDatabase:
    """Minimal in-memory Database mock for integration tests."""

    def __init__(self):
        self._tenants = {}
        self._api_keys = {}
        self._tenant_keys = {}
        self._usage_events = []

    async def get_tenant(self, tenant_id):
        return self._tenants.get(tenant_id)

    async def ensure_tenant(self, tenant_id, created_by=None):
        if tenant_id not in self._tenants:
            self._tenants[tenant_id] = {
                "id": tenant_id,
                "plan": "free",
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
                "stripe_sub_item_id": None,
                "created_by": created_by,
            }
        return self._tenants[tenant_id]

    async def update_tenant_plan(self, tenant_id, plan):
        if tenant_id in self._tenants:
            self._tenants[tenant_id]["plan"] = plan

    async def get_tenant_plan(self, tenant_id):
        t = self._tenants.get(tenant_id)
        return t["plan"] if t else "free"

    async def set_stripe_customer(self, tenant_id, customer_id):
        if tenant_id in self._tenants:
            self._tenants[tenant_id]["stripe_customer_id"] = customer_id

    async def find_tenant_by_stripe_customer(self, customer_id):
        for tid, t in self._tenants.items():
            if t.get("stripe_customer_id") == customer_id:
                return tid
        return None

    async def set_stripe_subscription(self, tenant_id, subscription_id, sub_item_id):
        if tenant_id in self._tenants:
            self._tenants[tenant_id]["stripe_subscription_id"] = subscription_id
            self._tenants[tenant_id]["stripe_sub_item_id"] = sub_item_id

    async def get_stripe_sub_item(self, tenant_id):
        t = self._tenants.get(tenant_id)
        return t.get("stripe_sub_item_id") if t else None

    async def create_api_key(self, user_id, tenant_id, name):
        import hashlib
        import secrets

        raw = secrets.token_urlsafe(32)
        plaintext = f"ac_{raw}"
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        data = {
            "key_hash": key_hash,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "name": name,
            "key_prefix": plaintext[:10],
            "revoked": False,
            "last_used_at": None,
        }
        self._api_keys[key_hash] = data
        self._tenant_keys.setdefault(tenant_id, []).append(key_hash)
        return plaintext, data

    async def get_api_key(self, key_hash):
        data = self._api_keys.get(key_hash)
        if data:
            import time

            data["last_used_at"] = time.time()
        return data

    async def list_api_keys(self, tenant_id):
        hashes = self._tenant_keys.get(tenant_id, [])
        return [self._api_keys[h] for h in hashes if h in self._api_keys]

    async def revoke_api_key(self, tenant_id, key_hash):
        data = self._api_keys.get(key_hash)
        if not data or data["tenant_id"] != tenant_id or data["revoked"]:
            return False
        data["revoked"] = True
        return True

    async def record_usage_event(
        self, tenant_id, user_id, session_id, event_type, duration_minutes=None, metadata=None
    ):
        self._usage_events.append(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "duration_minutes": duration_minutes,
            }
        )

    async def get_usage_stats(self, tenant_id):
        sessions = sum(
            1 for e in self._usage_events if e["tenant_id"] == tenant_id and e["event_type"] == "session_created"
        )
        minutes = sum(
            e["duration_minutes"] or 0
            for e in self._usage_events
            if e["tenant_id"] == tenant_id and e["event_type"] == "session_destroyed"
        )
        return {"total_sessions": sessions, "total_minutes": minutes}

    async def get_monthly_usage(self, tenant_id):
        return await self.get_usage_stats(tenant_id)


def make_hosted_config():
    """Create a HostedConfig mock with test values."""
    config = MagicMock()
    config.workos_api_key = "sk_test_workos"
    config.workos_client_id = "client_test"
    config.stripe_secret_key = "sk_test_stripe"
    config.stripe_webhook_secret = "whsec_test"
    config.redis_url = "redis://localhost:6379"
    return config


# ════════════════════════════════════════════════════════════════════
# Integration: Hosted providers implement OSS protocols
# ════════════════════════════════════════════════════════════════════


class TestHostedImplementsProtocols:
    """Verify hosted implementations satisfy runtime_checkable Protocol checks."""

    def test_workos_auth_is_auth_provider(self):
        redis = FakeRedis()
        config = make_hosted_config()
        auth = WorkOSAuthProvider(config, db=FakeDatabase(), redis=redis)
        assert isinstance(auth, AuthProvider)

    def test_redis_store_is_session_store(self):
        redis = FakeRedis()
        store = RedisSessionStore(redis)
        assert isinstance(store, SessionStore)

    def test_stripe_policy_is_session_policy(self):
        store = InMemorySessionStore()
        policy = StripePlanPolicy(store)
        assert isinstance(policy, SessionPolicy)

    def test_billing_hooks_is_lifecycle_hooks(self):
        redis = FakeRedis()
        hooks = BillingLifecycleHooks(redis)
        assert isinstance(hooks, LifecycleHooks)


# ════════════════════════════════════════════════════════════════════
# Integration: Hosted RelayConfig wires into create_app
# ════════════════════════════════════════════════════════════════════


class TestHostedRelayConfig:
    """Verify hosted providers plug into create_app correctly."""

    @pytest.fixture
    def redis(self):
        return FakeRedis()

    @pytest.fixture
    def hosted_config(self, redis):
        config = make_hosted_config()
        auth = WorkOSAuthProvider(config, db=FakeDatabase(), redis=redis)
        store = RedisSessionStore(redis)
        policy = StripePlanPolicy(store)
        hooks = BillingLifecycleHooks(redis)
        return RelayConfig(auth=auth, store=store, policy=policy, hooks=hooks)

    def test_create_app_with_hosted_config(self, hosted_config):
        app = create_app(hosted_config)
        assert app is not None
        assert app.state.relay_config is hosted_config

    @pytest_asyncio.fixture
    async def client(self, hosted_config):
        app = create_app(hosted_config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_health_endpoint(self, client):
        """Health endpoint works with hosted providers."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["sessions"] == 0

    @pytest.mark.asyncio
    async def test_sessions_requires_auth(self, client):
        """/sessions returns 401 without auth."""
        resp = await client.get("/sessions")
        assert resp.status_code == 401


# ════════════════════════════════════════════════════════════════════
# Integration: RedisSessionStore full lifecycle with create_app
# ════════════════════════════════════════════════════════════════════


class TestRedisStoreIntegration:
    """Test RedisSessionStore through the relay's store protocol."""

    @pytest.fixture
    def redis(self):
        return FakeRedis()

    @pytest.fixture
    def store(self, redis):
        return RedisSessionStore(redis)

    def _make_session(self, sid, tenant_id=None):
        return Session(
            agent_ws=MagicMock(),
            info=SessionInfo(session_id=sid, agent_framework="hermes"),
            session_secret="secret-123",
            user_id="user-1",
            tenant_id=tenant_id,
        )

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, store):
        """put → get → exists → count → list → remove → get returns None."""
        s = self._make_session("s1", "org-A")
        await store.put("s1", s)

        # Get
        got = await store.get("s1")
        assert got is not None
        assert got.info.session_id == "s1"
        assert got.tenant_id == "org-A"
        assert got.session_secret == "secret-123"

        # Exists
        assert await store.exists("s1") is True
        assert await store.exists("s2") is False

        # Count
        assert await store.count() == 1
        assert await store.count(tenant_id="org-A") == 1
        assert await store.count(tenant_id="org-B") == 0

        # List
        sessions = await store.list_for_tenant(tenant_id="org-A")
        assert len(sessions) == 1

        all_sessions = await store.list_for_tenant()
        assert len(all_sessions) == 1

        # Remove
        removed = await store.remove("s1")
        assert removed is not None
        assert removed.info.session_id == "s1"

        # After removal
        assert await store.get("s1") is None
        assert await store.exists("s1") is False
        assert await store.count() == 0

    @pytest.mark.asyncio
    async def test_multi_tenant_isolation(self, store):
        """Sessions from different tenants are isolated in listings."""
        await store.put("s1", self._make_session("s1", "org-A"))
        await store.put("s2", self._make_session("s2", "org-B"))
        await store.put("s3", self._make_session("s3", "org-A"))

        a_sessions = await store.list_for_tenant(tenant_id="org-A")
        assert len(a_sessions) == 2
        assert {s.info.session_id for s in a_sessions} == {"s1", "s3"}

        b_sessions = await store.list_for_tenant(tenant_id="org-B")
        assert len(b_sessions) == 1


# ════════════════════════════════════════════════════════════════════
# Integration: StripePlanPolicy enforces plan limits via relay protocol
# ════════════════════════════════════════════════════════════════════


class TestPlanPolicyIntegration:
    """Test StripePlanPolicy with RedisSessionStore."""

    @pytest.fixture
    def redis(self):
        return FakeRedis()

    @pytest.fixture
    def store(self, redis):
        return RedisSessionStore(redis)

    @pytest.fixture
    def policy(self, store):
        return StripePlanPolicy(store)

    def _make_session(self, sid, tenant_id):
        return Session(
            agent_ws=MagicMock(),
            info=SessionInfo(session_id=sid, agent_framework="hermes"),
            session_secret="secret",
            tenant_id=tenant_id,
        )

    @pytest.mark.asyncio
    async def test_free_plan_allows_up_to_3(self, store, policy):
        auth = AuthResult(authenticated=True, tenant_id="org-1", metadata={"plan": "free"})

        # First 3 should be allowed
        for i in range(3):
            ok, _ = await policy.can_create_session("user-1", "org-1", auth)
            assert ok is True
            await store.put(f"s{i}", self._make_session(f"s{i}", "org-1"))

        # 4th should be denied
        ok, err = await policy.can_create_session("user-1", "org-1", auth)
        assert ok is False
        assert "3" in err  # mentions the limit

    @pytest.mark.asyncio
    async def test_developer_plan_allows_up_to_10(self, store, policy):
        auth = AuthResult(authenticated=True, tenant_id="org-2", metadata={"plan": "developer"})

        for i in range(10):
            ok, _ = await policy.can_create_session("user-1", "org-2", auth)
            assert ok is True
            await store.put(f"s{i}", self._make_session(f"s{i}", "org-2"))

        ok, _err = await policy.can_create_session("user-1", "org-2", auth)
        assert ok is False

    @pytest.mark.asyncio
    async def test_pro_plan_unlimited(self, store, policy):
        auth = AuthResult(authenticated=True, tenant_id="org-3", metadata={"plan": "pro"})

        for i in range(50):
            await store.put(f"s{i}", self._make_session(f"s{i}", "org-3"))

        ok, _err = await policy.can_create_session("user-1", "org-3", auth)
        assert ok is True

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_limits(self, store, policy):
        """Org A's sessions don't count against Org B's limit."""
        # Fill org-A with 3 sessions
        for i in range(3):
            await store.put(f"a{i}", self._make_session(f"a{i}", "org-A"))

        # Org B should still be able to create sessions
        auth_b = AuthResult(authenticated=True, tenant_id="org-B", metadata={"plan": "free"})
        ok, _ = await policy.can_create_session("user-2", "org-B", auth_b)
        assert ok is True


# ════════════════════════════════════════════════════════════════════
# Integration: API key auth through WorkOSAuthProvider
# ════════════════════════════════════════════════════════════════════


class TestApiKeyAuthIntegration:
    """Test API key lifecycle: create → authenticate → revoke → fail."""

    @pytest.fixture
    def redis(self):
        return FakeRedis()

    @pytest.fixture
    def fake_db(self):
        return FakeDatabase()

    @pytest.fixture
    def auth(self, redis, fake_db):
        config = make_hosted_config()
        return WorkOSAuthProvider(config, db=fake_db, redis=redis)

    @pytest.mark.asyncio
    async def test_full_api_key_lifecycle(self, auth, fake_db):
        # Create a key
        plaintext, metadata = await auth.create_api_key("user-1", "org-1", "my-key")
        assert plaintext.startswith("ac_")
        assert metadata["name"] == "my-key"
        assert metadata["tenant_id"] == "org-1"

        # Set tenant plan via Postgres
        await fake_db.ensure_tenant("org-1")
        await fake_db.update_tenant_plan("org-1", "developer")

        # Authenticate with it
        result = await auth.authenticate_agent(plaintext, {})
        assert result.authenticated is True
        assert result.user_id == "user-1"
        assert result.tenant_id == "org-1"
        assert result.metadata["plan"] == "developer"

        # List keys
        keys = await auth.list_api_keys("org-1")
        assert len(keys) == 1
        assert keys[0]["name"] == "my-key"

        # Revoke
        key_hash = hash_api_key(plaintext)
        revoked = await auth.revoke_api_key("org-1", key_hash)
        assert revoked is True

        # Auth with revoked key fails
        result = await auth.authenticate_agent(plaintext, {})
        assert result.authenticated is False
        assert "revoked" in result.error.lower()

    @pytest.mark.asyncio
    async def test_wrong_tenant_cannot_revoke(self, auth):
        """Tenant isolation: org-B cannot revoke org-A's key."""
        plaintext, _ = await auth.create_api_key("user-1", "org-A", "key-a")
        key_hash = hash_api_key(plaintext)

        revoked = await auth.revoke_api_key("org-B", key_hash)
        assert revoked is False

        # Key still works
        result = await auth.authenticate_agent(plaintext, {})
        assert result.authenticated is True


# ════════════════════════════════════════════════════════════════════
# Integration: BillingHooks track usage through lifecycle
# ════════════════════════════════════════════════════════════════════


class TestBillingHooksIntegration:
    @pytest.fixture
    def redis(self):
        return FakeRedis()

    @pytest.fixture
    def fake_db(self):
        return FakeDatabase()

    @pytest.fixture
    def hooks(self, redis, fake_db):
        return BillingLifecycleHooks(redis, stripe=None, db=fake_db)

    @pytest.mark.asyncio
    async def test_session_lifecycle_tracking(self, hooks, redis, fake_db):
        """Full session lifecycle: created → usage tracked → destroyed → duration recorded."""
        # Session created
        await hooks.on_session_created("s1", "user-1", "org-1", {"plan": "developer"})

        # Verify Redis start time tracking
        start_time = await redis.hget("tenant:org-1:usage", "s1")
        assert start_time is not None
        assert float(start_time) > 0

        # Verify Postgres event recorded
        assert len(fake_db._usage_events) == 1
        assert fake_db._usage_events[0]["event_type"] == "session_created"

        # Session destroyed
        await hooks.on_session_destroyed("s1", "user-1", "org-1", "completed")

        # Verify Postgres event recorded with duration
        assert len(fake_db._usage_events) == 2
        destroyed_evt = fake_db._usage_events[1]
        assert destroyed_evt["event_type"] == "session_destroyed"
        assert destroyed_evt["duration_minutes"] >= 1

        # Redis usage tracking cleaned up
        remaining = await redis.hget("tenant:org-1:usage", "s1")
        assert remaining is None


# ════════════════════════════════════════════════════════════════════
# Integration: End-to-end hosted relay with all providers
# ════════════════════════════════════════════════════════════════════


class TestEndToEndHostedRelay:
    """
    Full integration: wire all hosted providers into create_app,
    then test HTTP endpoints behave correctly.
    """

    @pytest.fixture
    def redis(self):
        return FakeRedis()

    @pytest.fixture
    def hosted_app(self, redis):
        config_obj = make_hosted_config()
        auth = WorkOSAuthProvider(config_obj, db=FakeDatabase(), redis=redis)
        store = RedisSessionStore(redis)
        policy = StripePlanPolicy(store)
        hooks = BillingLifecycleHooks(redis, db=FakeDatabase())
        relay_config = RelayConfig(auth=auth, store=store, policy=policy, hooks=hooks)
        return create_app(relay_config)

    @pytest_asyncio.fixture
    async def client(self, hosted_app):
        transport = ASGITransport(app=hosted_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_health_returns_zero_sessions(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["sessions"] == 0

    @pytest.mark.asyncio
    async def test_sessions_rejects_no_auth(self, client):
        resp = await client.get("/sessions")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_sessions_rejects_api_key(self, client, redis):
        """
        API keys are for agent auth, not admin auth.
        Admin auth uses WorkOS session tokens (mocked here as failing).
        """
        config = make_hosted_config()
        auth = WorkOSAuthProvider(config, db=FakeDatabase(), redis=redis)
        plaintext, _ = await auth.create_api_key("user-1", "org-1", "test-key")

        # API keys are short — WorkOS validation should reject them
        resp = await client.get(
            "/sessions",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        # Should fail because API keys go through _validate_workos_session for admin auth
        assert resp.status_code in (401, 500)  # auth service unavailable or unauthorized
