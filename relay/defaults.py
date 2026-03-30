"""
Default (OSS) implementations of the relay protocols.

These are used when no custom configuration is provided to create_app().
They provide the same behavior as the original single-file relay:
  - Token-based auth via AGENT_TOKEN env var
  - In-memory session storage
  - Global session cap (no per-user/tenant limits)
  - No-op lifecycle hooks

In OSS mode, user_id and tenant_id are always None (single-tenant).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Any

from relay.models import Session
from relay.protocols import AuthResult

# Beta prefix pattern: axolotl_beta_ + 43 URL-safe base64 chars
_BETA_PREFIX = os.environ.get("AGENT_TOKEN_PREFIX", "")
_BETA_MIN_HASH_LEN = 43


# ─── Auth ───────────────────────────────────────────────────────────


class TokenAuthProvider:
    """
    OSS default: agents authenticate with a shared AGENT_TOKEN,
    viewers authenticate with per-session secrets.

    Supports two modes:
      1. Fixed token: AGENT_TOKEN=some-secret (exact match)
      2. Prefix token: AGENT_TOKEN_PREFIX=axolotl_beta_
         Accepts any token matching prefix + 43+ chars of hash.
         The full token becomes the user_id (per-agent session isolation).
    """

    def __init__(self, agent_token: str):
        self._agent_token = agent_token

    async def authenticate_agent(
        self,
        token: str | None,
        headers: dict[str, str],
    ) -> AuthResult:
        if not token:
            return AuthResult(authenticated=False, error="agent token required")

        # Mode 1: prefix-based tokens (beta)
        if _BETA_PREFIX and token.startswith(_BETA_PREFIX):
            suffix = token[len(_BETA_PREFIX) :]
            if len(suffix) >= _BETA_MIN_HASH_LEN and re.match(r"^[A-Za-z0-9_-]+$", suffix):
                # Use a hash of the token as user_id for session isolation
                user_id = hashlib.sha256(token.encode()).hexdigest()[:16]
                return AuthResult(authenticated=True, user_id=user_id)
            return AuthResult(
                authenticated=False,
                error=f"token must be {_BETA_PREFIX}<{_BETA_MIN_HASH_LEN}+ chars>",
            )

        # Mode 2: fixed token (OSS default)
        if self._agent_token and hmac.compare_digest(token.encode(), self._agent_token.encode()):
            return AuthResult(authenticated=True)

        # If only prefix mode is configured and no fixed token, reject
        if _BETA_PREFIX and not self._agent_token:
            return AuthResult(
                authenticated=False,
                error=f"token must start with {_BETA_PREFIX}",
            )

        return AuthResult(authenticated=False, error="invalid agent token")

    async def authenticate_viewer(
        self,
        session: Session,
        secret: str | None,
        headers: dict[str, str],
    ) -> AuthResult:
        if not secret:
            return AuthResult(authenticated=False, error="session secret required")
        if hmac.compare_digest(secret.encode(), session.session_secret.encode()):
            return AuthResult(authenticated=True)
        return AuthResult(authenticated=False, error="invalid session secret")

    async def authenticate_admin(
        self,
        token: str | None,
        headers: dict[str, str],
    ) -> AuthResult:
        # Admin uses the same agent token in OSS mode
        return await self.authenticate_agent(token, headers)


# ─── Session Storage ────────────────────────────────────────────────


class InMemorySessionStore:
    """OSS default: dict-based in-memory session storage."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def put(self, session_id: str, session: Session) -> None:
        self._sessions[session_id] = session

    async def remove(self, session_id: str) -> Session | None:
        return self._sessions.pop(session_id, None)

    async def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    async def count(self, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            return len(self._sessions)
        return sum(1 for s in self._sessions.values() if s.tenant_id == tenant_id)

    async def list_for_tenant(self, tenant_id: str | None = None) -> list[Session]:
        if tenant_id is None:
            return list(self._sessions.values())
        return [s for s in self._sessions.values() if s.tenant_id == tenant_id]

    async def get_expired(self, ttl_seconds: float) -> list[str]:
        now = time.time()
        return [sid for sid, s in self._sessions.items() if now - s.last_activity > ttl_seconds]


# ─── Session Policy ─────────────────────────────────────────────────


class DefaultSessionPolicy:
    """OSS default: global session cap, no per-user/tenant limits."""

    def __init__(self, max_sessions: int, store: Any):
        self._max_sessions = max_sessions
        self._store = store

    async def can_create_session(
        self,
        user_id: str | None,
        tenant_id: str | None,
        auth_result: AuthResult,
    ) -> tuple[bool, str | None]:
        total = await self._store.count()
        if total >= self._max_sessions:
            return (False, f"max sessions reached ({self._max_sessions})")
        return (True, None)

    def max_sessions_for_tenant(
        self,
        tenant_id: str | None,
        auth_result: AuthResult,
    ) -> int | None:
        return self._max_sessions


# ─── Lifecycle Hooks ────────────────────────────────────────────────


class NoopLifecycleHooks:
    """OSS default: no-op — all hooks are silent."""

    async def on_session_created(
        self,
        session_id: str,
        user_id: str | None,
        tenant_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        pass

    async def on_session_destroyed(
        self,
        session_id: str,
        user_id: str | None,
        tenant_id: str | None,
        reason: str,
    ) -> None:
        pass

    async def on_viewer_joined(
        self,
        session_id: str,
        tenant_id: str | None,
        viewer_count: int,
    ) -> None:
        pass

    async def on_viewer_left(
        self,
        session_id: str,
        tenant_id: str | None,
        viewer_count: int,
    ) -> None:
        pass
