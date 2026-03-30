"""
Agent Remote Control — Relay Server

A lightweight WebSocket relay that routes trace events from agents to viewers
and commands from viewers to agents. Self-hostable, security-first.

The relay is extensible via four protocol interfaces (see protocols.py):
  - AuthProvider     — authenticate agents, viewers, and admin requests
  - SessionStore     — persist session state (in-memory, Redis, DB, etc.)
  - SessionPolicy    — enforce session limits (global, per-user/plan, etc.)
  - LifecycleHooks   — react to session events (billing, analytics, etc.)

Usage (standalone / OSS):
    python -m relay                    # uses default config from env vars
    uvicorn relay.relay:app            # same, via uvicorn directly

Usage (hosted / custom config):
    from relay import create_app, RelayConfig
    app = create_app(RelayConfig(auth=..., store=..., policy=..., hooks=...))

Environment variables (used by the default OSS configuration):
    PORT                    — listen port (default: 8600)
    MAX_TRACE_LOG           — max trace events kept per session (default: 2000)
    MAX_SESSIONS            — max concurrent sessions (default: 100)
    MAX_VIEWERS_PER_SESSION — max viewers per session (default: 10)
    MAX_MESSAGE_SIZE        — max WebSocket message size in bytes (default: 1MB)
    SESSION_TTL_HOURS             — hours before idle sessions are cleaned up (default: 24)
    AGENT_DISCONNECTED_TTL_MINUTES — minutes to keep a session after agent disconnects (default: 5)
    ALLOWED_ORIGINS         — comma-separated allowed origins for CORS (default: "*")
    AGENT_TOKEN             — REQUIRED. Token agents present to register sessions.
                              If not set, the server auto-generates one and logs it.
    REQUIRE_TLS             — if "true", refuse non-TLS connections (default: "false")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from relay.defaults import (
    DefaultSessionPolicy,
    InMemorySessionStore,
    NoopLifecycleHooks,
    TokenAuthProvider,
)
from relay.models import Session, SessionInfo
from relay.protocols import (
    AuthProvider,
    LifecycleHooks,
    SessionPolicy,
    SessionStore,
)

# ─── Config ──────────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", "8600"))
MAX_TRACE_LOG = int(os.environ.get("MAX_TRACE_LOG", "2000"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "100"))
MAX_VIEWERS_PER_SESSION = int(os.environ.get("MAX_VIEWERS_PER_SESSION", "10"))
MAX_MESSAGE_SIZE = int(os.environ.get("MAX_MESSAGE_SIZE", str(1024 * 1024)))
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "24"))
# How long to keep a session alive after its agent disconnects (0 = clean up immediately on next pass)
AGENT_DISCONNECTED_TTL_MINUTES = int(os.environ.get("AGENT_DISCONNECTED_TTL_MINUTES", "5"))
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
REQUIRE_TLS = os.environ.get("REQUIRE_TLS", "false").lower() == "true"

AGENT_TOKEN: str = os.environ.get("AGENT_TOKEN", "")
if not AGENT_TOKEN:
    AGENT_TOKEN = secrets.token_urlsafe(32)
    _AGENT_TOKEN_GENERATED = True
else:
    _AGENT_TOKEN_GENERATED = False

logging.basicConfig(level=logging.INFO, format="[relay] %(message)s")
log = logging.getLogger("relay")


# ─── Relay Configuration ────────────────────────────────────────────


@dataclass
class RelayConfig:
    """
    Bundle of protocol implementations that define relay behavior.

    The hosted version provides its own implementations:
        config = RelayConfig(
            auth=WorkOSAuthProvider(...),
            store=RedisSessionStore(...),
            policy=StripePlanPolicy(...),
            hooks=BillingHooks(...),
        )
        app = create_app(config)
    """

    auth: AuthProvider
    store: SessionStore
    policy: SessionPolicy
    hooks: LifecycleHooks


# ─── Input Validation ───────────────────────────────────────────────

SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
MAX_FIELD_LEN = 256


def validate_session_id(sid: str) -> str | None:
    """Returns error message if invalid, None if valid."""
    if not sid:
        return "session ID is required"
    if not SESSION_ID_PATTERN.match(sid):
        return "session ID must be 1-128 chars of [a-zA-Z0-9_-]"
    return None


def sanitize_string(s: Any, max_len: int = MAX_FIELD_LEN) -> str | None:
    """Sanitize a string field. Returns None if input is not a string."""
    if not isinstance(s, str):
        return None
    return s[:max_len]


# ─── Rate Limiter ───────────────────────────────────────────────────

RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_HTTP = 60
RATE_LIMIT_MAX_WS_MSG = 120

_rate_counters: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(ip: str, limit: int) -> bool:
    """Returns True if within rate limit."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = _rate_counters[ip]
    _rate_counters[ip] = [t for t in timestamps if t > window_start]
    timestamps = _rate_counters[ip]
    if len(timestamps) >= limit:
        return False
    timestamps.append(now)
    return True


def extract_bearer_token(authorization: str | None) -> str | None:
    """Extract token from 'Bearer <token>' header."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


# ─── Helpers ────────────────────────────────────────────────────────


def get_client_ip(request: Request | None = None, ws: WebSocket | None = None) -> str:
    """Get client IP, respecting X-Forwarded-For behind reverse proxy."""
    source = request or ws
    if source is None:
        return "unknown"
    forwarded = source.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(source, "client", None)
    if client:
        return client.host
    return "unknown"


def ws_headers_dict(ws: WebSocket) -> dict[str, str]:
    """Convert WebSocket headers to a plain dict for protocol methods."""
    return {k: v for k, v in ws.headers.items()}


# ─── App Factory ────────────────────────────────────────────────────


def create_app(config: RelayConfig | None = None) -> FastAPI:
    """
    Create the relay FastAPI application.

    If no config is provided, uses the default OSS implementations
    configured from environment variables.
    """
    if config is None:
        store = InMemorySessionStore()
        config = RelayConfig(
            auth=TokenAuthProvider(AGENT_TOKEN),
            store=store,
            policy=DefaultSessionPolicy(MAX_SESSIONS, store),
            hooks=NoopLifecycleHooks(),
        )

    relay_app = FastAPI(
        title="Agent Remote Control Relay",
        version="0.3.0",
        docs_url=None,
        redoc_url=None,
    )

    relay_app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Store config on app state for access in route handlers
    relay_app.state.relay_config = config

    # ── Session cleanup task ────────────────────────────────────

    async def cleanup_expired_sessions():
        while True:
            await asyncio.sleep(60)
            try:
                ttl_seconds = SESSION_TTL_HOURS * 3600
                expired = await config.store.get_expired(ttl_seconds)
            except Exception as exc:
                log.error("cleanup: failed to get expired sessions: %s", exc)
                continue

            # Also expire sessions whose agent has disconnected
            if AGENT_DISCONNECTED_TTL_MINUTES >= 0:
                disconnected_ttl = AGENT_DISCONNECTED_TTL_MINUTES * 60
                now = time.time()
                try:
                    all_sessions = await config.store.list_for_tenant(tenant_id=None)
                    for s in all_sessions:
                        if s.info.session_id in expired:
                            continue
                        agent_gone = (
                            s.agent_ws is None
                            or s.agent_ws.client_state == WebSocketState.DISCONNECTED
                        )
                        if agent_gone and (now - s.last_activity) >= disconnected_ttl:
                            expired.append(s.info.session_id)
                except Exception as exc:
                    log.error("cleanup: failed to check disconnected sessions: %s", exc)

            for sid in expired:
                session = await config.store.remove(sid)
                if session:
                    agent_gone = (
                        session.agent_ws is None
                        or session.agent_ws.client_state == WebSocketState.DISCONNECTED
                    )
                    reason = "agent_disconnected" if agent_gone else "expired"
                    log.info("session cleaned up: %s (%s)", sid, reason)
                    await config.hooks.on_session_destroyed(sid, session.user_id, session.tenant_id, reason)
                    try:
                        await session.agent_ws.close(code=4002, reason="session expired")
                    except Exception:
                        pass
                    for v in session.viewers:
                        try:
                            await v.close(code=4002, reason="session expired")
                        except Exception:
                            pass

            # Clean up stale rate limiter entries and cap total size
            now = time.time()
            window_start = now - RATE_LIMIT_WINDOW
            stale = [ip for ip, ts in _rate_counters.items() if not ts or ts[-1] < window_start]
            for ip in stale:
                del _rate_counters[ip]
            # Hard cap: evict oldest entries if dict grows too large
            MAX_RATE_ENTRIES = 10_000
            if len(_rate_counters) > MAX_RATE_ENTRIES:
                sorted_keys = sorted(_rate_counters, key=lambda k: _rate_counters[k][-1] if _rate_counters[k] else 0)
                for k in sorted_keys[: len(_rate_counters) - MAX_RATE_ENTRIES]:
                    del _rate_counters[k]

    @relay_app.on_event("startup")
    async def startup():
        asyncio.create_task(cleanup_expired_sessions())

    # ── HTTP Endpoints ──────────────────────────────────────────

    @relay_app.get("/")
    async def root():
        """Redirect root to /viewer if available, else return health."""
        if _web_client_dir:
            from fastapi.responses import RedirectResponse

            return RedirectResponse("/viewer")
        total = await config.store.count()
        return {"status": "ok", "sessions": total}

    @relay_app.get("/health")
    async def health():
        total = await config.store.count()
        return {"status": "ok", "sessions": total}

    @relay_app.get("/sessions")
    async def list_sessions(request: Request, authorization: str | None = Header(None)):
        ip = get_client_ip(request=request)
        if not check_rate_limit(ip, RATE_LIMIT_MAX_HTTP):
            return JSONResponse({"error": "rate limited"}, status_code=429)

        token = extract_bearer_token(authorization)
        headers = {k: v for k, v in request.headers.items()}
        auth_result = await config.auth.authenticate_admin(token, headers)
        if not auth_result.authenticated:
            return JSONResponse(
                {"error": auth_result.error or "unauthorized"},
                status_code=401,
            )

        # Scope listing: by tenant (hosted), by user_id (beta prefix tokens),
        # or empty (shared fixed token — prevent enumeration).
        if auth_result.tenant_id:
            all_sessions = await config.store.list_for_tenant(tenant_id=auth_result.tenant_id)
        elif auth_result.user_id:
            all_sessions = [
                s for s in await config.store.list_for_tenant(tenant_id=None) if s.user_id == auth_result.user_id
            ]
        else:
            return []

        def session_dict(s: Session) -> dict:
            d = s.info.to_dict()
            d["agentConnected"] = (
                s.agent_ws is not None
                and s.agent_ws.client_state != WebSocketState.DISCONNECTED
            )
            d["viewerCount"] = len(s.viewers)
            d["lastActivity"] = s.last_activity
            return d

        return [session_dict(s) for s in all_sessions]

    @relay_app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request, authorization: str | None = Header(None)):
        ip = get_client_ip(request=request)
        if not check_rate_limit(ip, RATE_LIMIT_MAX_HTTP):
            return JSONResponse({"error": "rate limited"}, status_code=429)

        token = extract_bearer_token(authorization)
        headers = {k: v for k, v in request.headers.items()}
        auth_result = await config.auth.authenticate_agent(token, headers)
        if not auth_result.authenticated:
            return JSONResponse(
                {"error": auth_result.error or "unauthorized"},
                status_code=401,
            )

        session = await config.store.get(session_id)
        if not session:
            return JSONResponse({"error": "session not found"}, status_code=404)

        # Only allow deleting sessions owned by this user/tenant
        if auth_result.user_id and session.user_id != auth_result.user_id:
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        if auth_result.tenant_id and session.tenant_id != auth_result.tenant_id:
            return JSONResponse({"error": "unauthorized"}, status_code=403)

        await config.store.remove(session_id)
        await config.hooks.on_session_destroyed(
            session_id, session.user_id, session.tenant_id, "deleted_by_user"
        )
        log.info("session deleted by user: %s", session_id)
        return {"deleted": session_id}

    # ── Web Viewer (static SPA) ──────────────────────────────────

    # Serve the web-client SPA if the dist directory exists.
    # This allows self-hosted users to open the viewer URL directly.
    # The SPA is mounted AFTER all API/WS routes so it doesn't shadow them.
    _web_client_dir = None
    for candidate in [
        Path(__file__).parent.parent / "packages" / "web-client" / "dist",  # repo checkout
        Path(__file__).parent / "web-client",  # packaged alongside relay
    ]:
        if candidate.is_dir() and (candidate / "index.html").exists():
            _web_client_dir = candidate
            break

    # ── WebSocket Relay ─────────────────────────────────────────

    @relay_app.websocket("/ws")
    async def websocket_relay(ws: WebSocket):
        # Origin validation
        origin = ws.headers.get("origin", "")
        if ALLOWED_ORIGINS != ["*"] and origin:
            if origin not in ALLOWED_ORIGINS:
                await ws.accept()
                await ws.close(code=4003, reason="origin not allowed")
                return

        # TLS enforcement
        if REQUIRE_TLS:
            forwarded_proto = ws.headers.get("x-forwarded-proto", "")
            scheme = ws.scope.get("scheme", "")
            if forwarded_proto != "https" and scheme != "wss":
                await ws.accept()
                await ws.close(code=4004, reason="TLS required")
                return

        await ws.accept()

        bound_session_id: str | None = None
        role: str | None = None
        authenticated = False
        ip = get_client_ip(ws=ws)

        try:
            async for raw in ws.iter_text():
                # Message size check
                if len(raw) > MAX_MESSAGE_SIZE:
                    await ws.send_json({"error": "message too large"})
                    continue

                # Rate limit
                if not check_rate_limit(ip, RATE_LIMIT_MAX_WS_MSG):
                    await ws.send_json({"error": "rate limited"})
                    continue

                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json({"error": "invalid JSON"})
                    continue

                if not isinstance(envelope, dict):
                    await ws.send_json({"error": "expected JSON object"})
                    continue

                kind = envelope.get("kind")

                # ── Agent registers its session ──
                if kind == "register":
                    session_data = envelope.get("session", {})
                    if not isinstance(session_data, dict):
                        await ws.send_json({"error": "invalid session data"})
                        continue

                    # Authenticate agent
                    auth_token = envelope.get("token")
                    auth_result = await config.auth.authenticate_agent(
                        auth_token,
                        ws_headers_dict(ws),
                    )
                    if not auth_result.authenticated:
                        await ws.send_json({"error": auth_result.error or "unauthorized"})
                        await ws.close(code=4001, reason="unauthorized")
                        return

                    sid = sanitize_string(session_data.get("sessionId"), 128) or ""
                    err = validate_session_id(sid)
                    if err:
                        await ws.send_json({"error": err})
                        continue

                    # Re-registration: if same session ID exists and same user, take it over
                    # (agent reconnected after relay restart or network drop)
                    existing = await config.store.get(sid)
                    if existing:
                        # Only allow takeover by the same user (or in OSS mode where user_id is None)
                        if existing.user_id is not None and existing.user_id != auth_result.user_id:
                            await ws.send_json({"error": "session owned by another user"})
                            continue
                        # Reject if the existing agent WS is still connected (true duplicate, not a reconnect).
                        # agent_ws may be None if the Redis store cleared it after disconnect.
                        agent_ws_alive = (
                            existing.agent_ws is not None
                            and existing.agent_ws.client_state == WebSocketState.CONNECTED
                        )
                        if agent_ws_alive:
                            await ws.send_json({"error": f"session '{sid}' already exists"})
                            continue
                        # Take over: close old agent WS (if any), reuse session secret, keep viewers
                        try:
                            if existing.agent_ws is not None:
                                await existing.agent_ws.close(code=4010, reason="session taken over by reconnect")
                        except Exception:
                            pass
                        existing.agent_ws = ws
                        existing.last_activity = time.time()
                        await config.store.put(sid, existing)
                        role = "agent"
                        authenticated = True
                        bound_session_id = sid
                        await ws.send_json(
                            {
                                "kind": "registered",
                                "sessionId": sid,
                                "sessionSecret": existing.session_secret,  # Reuse original secret!
                            }
                        )
                        log.info("agent re-registered (takeover): %s", sid)
                        continue

                    # Check policy (plan limits, etc.)
                    allowed, deny_reason = await config.policy.can_create_session(
                        auth_result.user_id,
                        auth_result.tenant_id,
                        auth_result,
                    )
                    if not allowed:
                        await ws.send_json({"error": deny_reason or "session limit reached"})
                        await ws.close(code=4005, reason="session limit reached")
                        return

                    # Create session — use agent-proposed secret if provided (allows
                    # viewer URLs to survive relay restarts), otherwise generate one.
                    proposed_secret = sanitize_string(session_data.get("sessionSecret"), 64)
                    session_secret = (
                        proposed_secret
                        if proposed_secret and len(proposed_secret) >= 16
                        else secrets.token_urlsafe(32)
                    )
                    session = Session(
                        agent_ws=ws,
                        info=SessionInfo(
                            session_id=sid,
                            agent_framework=sanitize_string(session_data.get("agentFramework"), 32) or "unknown",
                            agent_name=sanitize_string(session_data.get("agentName"), 128),
                            started_at=sanitize_string(session_data.get("startedAt"), 64) or "",
                            e2e=sanitize_string(session_data.get("e2e"), 32) or None,
                        ),
                        session_secret=session_secret,
                        user_id=auth_result.user_id,
                        tenant_id=auth_result.tenant_id,
                    )
                    await config.store.put(sid, session)

                    bound_session_id = sid
                    role = "agent"
                    authenticated = True

                    # Notify lifecycle
                    await config.hooks.on_session_created(
                        sid,
                        auth_result.user_id,
                        auth_result.tenant_id,
                        auth_result.metadata,
                    )

                    # Return session secret to agent operator
                    await ws.send_json(
                        {
                            "kind": "registered",
                            "sessionId": sid,
                            "sessionSecret": session_secret,
                        }
                    )
                    log.info("agent registered: %s (%s)", sid, session.info.agent_framework)

                # ── Agent sends trace → forward to viewers ──
                elif kind == "trace":
                    if role != "agent" or not authenticated:
                        await ws.send_json({"error": "not authorized as agent"})
                        continue

                    event = envelope.get("event", {})
                    if not isinstance(event, dict):
                        continue

                    # Validate individual field sizes (prevent memory exhaustion)
                    MAX_FIELD_SIZE = 100_000  # 100KB per string field
                    oversized = False
                    for key, value in event.items():
                        if isinstance(value, str) and len(value) > MAX_FIELD_SIZE:
                            await ws.send_json({"error": f"trace field too large: {key}"})
                            oversized = True
                            break
                    if oversized:
                        continue

                    sid = bound_session_id
                    session = await config.store.get(sid) if sid else None
                    if not session:
                        continue

                    session.last_activity = time.time()
                    if hasattr(config.store, "update_activity"):
                        await config.store.update_activity(sid)

                    # Store trace (with size cap)
                    session.traces.append(envelope)
                    if len(session.traces) > MAX_TRACE_LOG:
                        session.traces = session.traces[-MAX_TRACE_LOG:]

                    # Forward to viewers — via pub/sub if available (multi-instance),
                    # otherwise direct local forwarding (single-instance / OSS)
                    if hasattr(config.store, "publish_trace"):
                        await config.store.publish_trace(sid, envelope)
                    elif session.viewers:
                        payload = json.dumps(envelope)
                        dead: list[WebSocket] = []
                        for viewer in session.viewers:
                            try:
                                await viewer.send_text(payload)
                            except Exception:
                                dead.append(viewer)
                        for d in dead:
                            session.viewers.discard(d)

                # ── Viewer subscribes to a session ──
                elif kind == "subscribe":
                    sid = sanitize_string(envelope.get("sessionId"), 128) or ""
                    viewer_secret = envelope.get("sessionSecret", "")

                    err = validate_session_id(sid)
                    if err:
                        await ws.send_json({"error": err})
                        continue

                    # Per-session brute-force protection (only counts auth failures,
                    # not "session not found" which happens during reconnect)
                    sub_key = f"sub:{sid}"
                    now_ts = time.time()
                    _rate_counters[sub_key] = [t for t in _rate_counters.get(sub_key, []) if now_ts - t < 300]
                    if len(_rate_counters[sub_key]) >= 30:
                        await ws.send_json({"error": "too many failed subscription attempts"})
                        continue

                    # Fetch session first, then authenticate viewer
                    session = await config.store.get(sid)
                    if not session:
                        # Don't count "not found" against rate limit — this happens
                        # during reconnect when agent hasn't re-registered yet
                        await ws.send_json({"error": "session not found"})
                        continue

                    auth_result = await config.auth.authenticate_viewer(
                        session,
                        viewer_secret,
                        ws_headers_dict(ws),
                    )
                    if not auth_result.authenticated:
                        _rate_counters[sub_key].append(now_ts)
                        await ws.send_json({"error": auth_result.error or "invalid session secret"})
                        continue

                    # Enforce viewer limit
                    if len(session.viewers) >= MAX_VIEWERS_PER_SESSION:
                        await ws.send_json({"error": "max viewers reached for this session"})
                        continue

                    bound_session_id = sid
                    role = "viewer"
                    authenticated = True
                    session.viewers.add(ws)
                    session.last_activity = time.time()
                    if hasattr(config.store, "update_activity"):
                        await config.store.update_activity(sid)
                    log.info("viewer subscribed: %s", sid)

                    await config.hooks.on_viewer_joined(sid, session.tenant_id, len(session.viewers))

                    # Send session info + replay traces
                    await ws.send_json(
                        {
                            "kind": "register",
                            "session": session.info.to_dict(),
                        }
                    )
                    for entry in session.traces:
                        await ws.send_json(entry)

                # ── Viewer sends command → forward to agent ──
                elif kind == "command":
                    if role != "viewer" or not authenticated:
                        await ws.send_json({"error": "not authorized as viewer"})
                        continue

                    command = envelope.get("command", {})
                    if not isinstance(command, dict):
                        continue

                    sid = bound_session_id
                    session = await config.store.get(sid) if sid else None
                    if not session:
                        await ws.send_json({"error": "session not found"})
                        continue

                    session.last_activity = time.time()
                    if hasattr(config.store, "update_activity"):
                        await config.store.update_activity(sid)

                    # Validate command type (skip for E2E encrypted commands — relay can't read them)
                    if not envelope.get("encrypted"):
                        cmd_type = command.get("type", "")
                        if cmd_type not in ("inject_message", "cancel", "approve_tool", "deny_tool"):
                            await ws.send_json({"error": "unknown command type"})
                            continue

                    # Forward to agent — via pub/sub if available (multi-instance),
                    # otherwise direct local forwarding (single-instance / OSS)
                    if hasattr(config.store, "publish_command"):
                        await config.store.publish_command(sid, envelope)
                    else:
                        try:
                            await session.agent_ws.send_json(envelope)
                        except Exception:
                            await ws.send_json({"error": "agent not connected"})

                elif kind == "ping":
                    await ws.send_json({"kind": "pong"})

                else:
                    await ws.send_json({"error": "unknown message kind"})

        except WebSocketDisconnect:
            pass
        finally:
            if role == "agent" and bound_session_id:
                session = await config.store.get(bound_session_id)
                if session:
                    log.info("agent disconnected: %s", bound_session_id)

                    # Clear agent WebSocket but keep session metadata in store
                    # so the secret is preserved for reconnecting viewers.
                    # Only fully remove if the store doesn't support persistence.
                    if hasattr(config.store, "publish_trace"):
                        # Redis store: keep metadata, clear agent_ws from runtime
                        runtime = config.store._runtimes.get(bound_session_id)
                        if runtime:
                            runtime.agent_ws = None
                    else:
                        # In-memory store: remove entirely (no persistence)
                        await config.store.remove(bound_session_id)

                    await config.hooks.on_session_destroyed(
                        bound_session_id,
                        session.user_id,
                        session.tenant_id,
                        "agent_disconnected",
                    )

                    # Notify viewers (via pub/sub or direct)
                    disconnect_envelope = {
                        "kind": "trace",
                        "event": {
                            "id": f"disconnect-{int(time.time() * 1000)}",
                            "sessionId": bound_session_id,
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                            "type": "status_change",
                            "status": "error",
                            "detail": "Agent disconnected",
                        },
                    }
                    if hasattr(config.store, "publish_trace"):
                        await config.store.publish_trace(bound_session_id, disconnect_envelope)
                    else:
                        disconnect_msg = json.dumps(disconnect_envelope)
                        for viewer in session.viewers:
                            try:
                                await viewer.send_text(disconnect_msg)
                            except Exception:
                                pass

            if role == "viewer" and bound_session_id:
                session = await config.store.get(bound_session_id)
                if session:
                    session.viewers.discard(ws)
                    await config.hooks.on_viewer_left(
                        bound_session_id,
                        session.tenant_id,
                        len(session.viewers),
                    )

    # Mount static web viewer LAST so it doesn't shadow API/WS routes.
    # Uses StaticFiles with html=True for proper SPA fallback to index.html.
    if _web_client_dir:
        relay_app.mount(
            "/viewer",
            StaticFiles(directory=str(_web_client_dir), html=True),
            name="viewer",
        )
        log.info("Web viewer: /viewer (serving from %s)", _web_client_dir)

    return relay_app


# ─── Default module-level app (for `uvicorn relay.relay:app`) ───────

app = create_app()


# ─── CLI entrypoint ─────────────────────────────────────────────────


def main():
    import uvicorn

    log.info("Starting relay server on :%d", PORT)
    log.info("WebSocket: ws://localhost:%d/ws", PORT)
    log.info("HTTP API:  http://localhost:%d/sessions", PORT)
    log.info("Max sessions: %d, Max trace log: %d, Session TTL: %dh, Disconnected TTL: %dm",
             MAX_SESSIONS, MAX_TRACE_LOG, SESSION_TTL_HOURS, AGENT_DISCONNECTED_TTL_MINUTES)
    if _AGENT_TOKEN_GENERATED:
        log.info("=" * 60)
        log.info("  AGENT_TOKEN (auto-generated): %s", AGENT_TOKEN)
        log.info("  Set AGENT_TOKEN env var to use a fixed token.")
        log.info("=" * 60)
    else:
        log.info("Agent token: configured via AGENT_TOKEN env var")
    if REQUIRE_TLS:
        log.info("TLS: required")
    else:
        log.warning("TLS: not required (set REQUIRE_TLS=true for production)")

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
