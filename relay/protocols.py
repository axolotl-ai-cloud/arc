"""
Extension protocols for the relay server.

The open-source relay ships with default implementations (see defaults.py).
A hosted/commercial version can provide alternative implementations to add
WorkOS auth, Stripe billing, Redis persistence, etc.

Usage (hosted version):
    from relay import create_app, RelayConfig
    from your_hosted_pkg.auth import WorkOSAuthProvider
    from your_hosted_pkg.store import RedisSessionStore
    from your_hosted_pkg.policy import StripePlanPolicy
    from your_hosted_pkg.hooks import BillingHooks

    config = RelayConfig(
        auth=WorkOSAuthProvider(...),
        store=RedisSessionStore(...),
        policy=StripePlanPolicy(...),
        hooks=BillingHooks(...),
    )
    app = create_app(config)

Multi-tenancy:
    The hosted relay is multi-user and multi-tenant. Every authenticated
    identity carries both a `user_id` (individual) and a `tenant_id`
    (organization/team). Plan limits and session visibility are scoped to
    the tenant; audit logs record the individual user.

    In OSS mode both are None (single-tenant, no user concept).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from relay.models import Session

# ─── Auth ───────────────────────────────────────────────────────────


@dataclass
class AuthResult:
    """Result of an authentication attempt."""

    authenticated: bool
    user_id: str | None = None  # Individual user (for audit trails)
    tenant_id: str | None = None  # Organization/team (for plan limits, isolation)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Convenience: hosted auth can stash plan info here
    # e.g. metadata={"plan": "developer", "plan_session_limit": 10}


@runtime_checkable
class AuthProvider(Protocol):
    """Authenticates agents, viewers, and admin API callers."""

    async def authenticate_agent(
        self,
        token: str | None,
        headers: dict[str, str],
    ) -> AuthResult:
        """
        Authenticate an agent registering a session.

        In OSS mode, verifies the shared AGENT_TOKEN.
        In hosted mode, validates a WorkOS JWT/API key and returns the
        user_id and tenant_id (org) the agent belongs to.
        """
        ...

    async def authenticate_viewer(
        self,
        session: Session,
        secret: str | None,
        headers: dict[str, str],
    ) -> AuthResult:
        """
        Authenticate a viewer subscribing to a session.

        The relay fetches the session first and passes it here so the provider
        can check the session secret (OSS) or validate a JWT AND verify the
        viewer belongs to the same tenant as the session owner (hosted).

        Multi-tenant hosted implementations MUST verify that the viewer's
        tenant_id matches session.tenant_id to enforce tenant isolation.
        """
        ...

    async def authenticate_admin(
        self,
        token: str | None,
        headers: dict[str, str],
    ) -> AuthResult:
        """
        Authenticate admin HTTP requests (e.g., GET /sessions).

        The returned tenant_id is used to scope the session listing —
        admins only see sessions belonging to their tenant.
        """
        ...


# ─── Session Storage ────────────────────────────────────────────────


@runtime_checkable
class SessionStore(Protocol):
    """
    Persistence layer for session state.

    Multi-tenant operations use tenant_id for scoping:
      - count(tenant_id=) counts sessions for a specific tenant
      - list_for_tenant() returns sessions visible to a tenant
      - The hosted Redis/DB implementation can use tenant-prefixed keys
    """

    async def get(self, session_id: str) -> Session | None: ...

    async def put(self, session_id: str, session: Session) -> None: ...

    async def remove(self, session_id: str) -> Session | None: ...

    async def exists(self, session_id: str) -> bool: ...

    async def count(self, tenant_id: str | None = None) -> int:
        """
        Count active sessions.

        If tenant_id is provided, count only that tenant's sessions.
        If None, count all sessions (used by OSS and global health checks).
        """
        ...

    async def list_for_tenant(self, tenant_id: str | None = None) -> list[Session]:
        """
        List sessions visible to a tenant.

        If tenant_id is None, returns all sessions (OSS single-tenant mode).
        If tenant_id is provided, returns only sessions belonging to that tenant.
        """
        ...

    async def get_expired(self, ttl_seconds: float) -> list[str]:
        """Return session IDs that have been idle beyond ttl_seconds."""
        ...


# ─── Session Policy ─────────────────────────────────────────────────


@runtime_checkable
class SessionPolicy(Protocol):
    """
    Decides whether a user/tenant can create a session.

    In multi-tenant mode, plan limits apply per-tenant:
      - Free (tinkerer): 3 active sessions across the org
      - Developer: 10 active sessions across the org
      - Pro: unlimited
    """

    async def can_create_session(
        self,
        user_id: str | None,
        tenant_id: str | None,
        auth_result: AuthResult,
    ) -> tuple[bool, str | None]:
        """
        Check whether this user/tenant may create a new session.
        Returns (allowed, error_message_if_denied).

        Hosted implementation counts sessions per tenant_id and compares
        against the plan limit from auth_result.metadata.
        """
        ...

    def max_sessions_for_tenant(
        self,
        tenant_id: str | None,
        auth_result: AuthResult,
    ) -> int | None:
        """
        Return the session cap for this tenant, or None for unlimited.
        Informational — used in API responses and error messages.
        """
        ...


# ─── Lifecycle Hooks ────────────────────────────────────────────────


@runtime_checkable
class LifecycleHooks(Protocol):
    """
    Called at key moments in a session's lifecycle.

    The hosted version uses these for:
      - Stripe metered billing (on_session_created/destroyed)
      - Analytics and usage tracking
      - Tenant-level dashboards

    Both user_id and tenant_id are provided so hooks can attribute
    events to both the individual (audit) and the org (billing).
    """

    async def on_session_created(
        self,
        session_id: str,
        user_id: str | None,
        tenant_id: str | None,
        metadata: dict[str, Any],
    ) -> None: ...

    async def on_session_destroyed(
        self,
        session_id: str,
        user_id: str | None,
        tenant_id: str | None,
        reason: str,
    ) -> None: ...

    async def on_viewer_joined(
        self,
        session_id: str,
        tenant_id: str | None,
        viewer_count: int,
    ) -> None: ...

    async def on_viewer_left(
        self,
        session_id: str,
        tenant_id: str | None,
        viewer_count: int,
    ) -> None: ...
