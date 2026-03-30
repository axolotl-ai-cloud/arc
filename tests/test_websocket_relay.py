"""
WebSocket Relay Integration Tests

End-to-end tests for the critical data flows through the relay:
  1. Agent registers → relay acknowledges with session secret
  2. Viewer subscribes with secret → relay replays traces
  3. Agent sends trace → relay forwards to viewer
  4. Viewer sends command → relay forwards to agent
  5. Agent disconnects → viewer notified
  6. Role enforcement (viewer can't send traces, agent can't send commands)
  7. Auth failures (wrong token, wrong secret)

Uses Starlette's TestClient which provides synchronous WebSocket testing
against the ASGI app without needing a running server.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from relay.defaults import (
    DefaultSessionPolicy,
    InMemorySessionStore,
    NoopLifecycleHooks,
    TokenAuthProvider,
)
from relay.relay import RelayConfig, create_app

# Fixed token for tests
TEST_TOKEN = "test-token-12345"


@pytest.fixture
def app():
    """Create a fresh relay app for each test."""
    store = InMemorySessionStore()
    config = RelayConfig(
        auth=TokenAuthProvider(TEST_TOKEN),
        store=store,
        policy=DefaultSessionPolicy(100, store),
        hooks=NoopLifecycleHooks(),
    )
    return create_app(config)


@pytest.fixture
def client(app):
    return TestClient(app)


def register_agent(ws, session_id="test-session", token=TEST_TOKEN):
    """Helper: register an agent and return the session secret."""
    ws.send_json(
        {
            "kind": "register",
            "token": token,
            "session": {
                "sessionId": session_id,
                "agentFramework": "hermes",
                "agentName": "test-agent",
                "startedAt": "2025-01-01T00:00:00Z",
            },
        }
    )
    resp = ws.receive_json()
    assert resp["kind"] == "registered", f"Expected 'registered', got: {resp}"
    assert resp["sessionId"] == session_id
    assert "sessionSecret" in resp
    return resp["sessionSecret"]


def subscribe_viewer(ws, session_id="test-session", session_secret=""):
    """Helper: subscribe a viewer and return the session info."""
    ws.send_json(
        {
            "kind": "subscribe",
            "sessionId": session_id,
            "sessionSecret": session_secret,
        }
    )
    resp = ws.receive_json()
    assert resp["kind"] == "register", f"Expected session info, got: {resp}"
    return resp


# ════════════════════════════════════════════════════════════════════
# Agent Registration
# ════════════════════════════════════════════════════════════════════


class TestAgentRegistration:
    def test_agent_registers_successfully(self, client):
        with client.websocket_connect("/ws") as ws:
            secret = register_agent(ws)
            assert len(secret) > 0

    def test_agent_registration_returns_session_secret(self, client):
        with client.websocket_connect("/ws") as ws:
            secret = register_agent(ws)
            # Session secret should be a URL-safe base64 string
            assert isinstance(secret, str)
            assert len(secret) >= 32

    def test_agent_registration_fails_with_wrong_token(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "kind": "register",
                    "token": "wrong-token",
                    "session": {
                        "sessionId": "test-session",
                        "agentFramework": "hermes",
                        "startedAt": "2025-01-01T00:00:00Z",
                    },
                }
            )
            resp = ws.receive_json()
            assert "error" in resp

    def test_agent_registration_fails_without_token(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "kind": "register",
                    "session": {
                        "sessionId": "test-session",
                        "agentFramework": "hermes",
                        "startedAt": "2025-01-01T00:00:00Z",
                    },
                }
            )
            resp = ws.receive_json()
            assert "error" in resp

    def test_duplicate_session_id_rejected(self, client):
        with client.websocket_connect("/ws") as agent1:
            register_agent(agent1, session_id="dup-session")

            with client.websocket_connect("/ws") as agent2:
                agent2.send_json(
                    {
                        "kind": "register",
                        "token": TEST_TOKEN,
                        "session": {
                            "sessionId": "dup-session",
                            "agentFramework": "hermes",
                            "startedAt": "2025-01-01T00:00:00Z",
                        },
                    }
                )
                resp = agent2.receive_json()
                assert "error" in resp
                assert "already exists" in resp["error"]


# ════════════════════════════════════════════════════════════════════
# Viewer Subscription
# ════════════════════════════════════════════════════════════════════


class TestViewerSubscription:
    def test_viewer_subscribes_with_correct_secret(self, client):
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                info = subscribe_viewer(viewer, "test-session", secret)
                assert info["session"]["sessionId"] == "test-session"
                assert info["session"]["agentFramework"] == "hermes"

    def test_viewer_rejected_with_wrong_secret(self, client):
        with client.websocket_connect("/ws") as agent:
            register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                viewer.send_json(
                    {
                        "kind": "subscribe",
                        "sessionId": "test-session",
                        "sessionSecret": "wrong-secret",
                    }
                )
                resp = viewer.receive_json()
                assert "error" in resp

    def test_viewer_rejected_with_no_secret(self, client):
        with client.websocket_connect("/ws") as agent:
            register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                viewer.send_json(
                    {
                        "kind": "subscribe",
                        "sessionId": "test-session",
                        "sessionSecret": "",
                    }
                )
                resp = viewer.receive_json()
                assert "error" in resp

    def test_viewer_rejected_for_nonexistent_session(self, client):
        with client.websocket_connect("/ws") as viewer:
            viewer.send_json(
                {
                    "kind": "subscribe",
                    "sessionId": "nonexistent",
                    "sessionSecret": "anything",
                }
            )
            resp = viewer.receive_json()
            assert "error" in resp
            assert "not found" in resp["error"]

    def test_viewer_receives_replayed_traces_on_subscribe(self, client):
        """Viewer subscribing to an active session should get all prior traces."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            # Agent sends some traces BEFORE viewer connects
            trace1 = {
                "kind": "trace",
                "event": {
                    "id": "evt-1",
                    "sessionId": "test-session",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "type": "agent_message",
                    "role": "assistant",
                    "content": "Hello from agent",
                },
            }
            trace2 = {
                "kind": "trace",
                "event": {
                    "id": "evt-2",
                    "sessionId": "test-session",
                    "timestamp": "2025-01-01T00:00:02Z",
                    "type": "tool_call",
                    "toolName": "search",
                    "toolInput": {"query": "test"},
                    "status": "started",
                },
            }
            agent.send_json(trace1)
            agent.send_json(trace2)

            # Now viewer subscribes
            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                # Viewer should receive the two traces as replay
                replayed1 = viewer.receive_json()
                replayed2 = viewer.receive_json()

                assert replayed1["kind"] == "trace"
                assert replayed1["event"]["id"] == "evt-1"
                assert replayed1["event"]["content"] == "Hello from agent"

                assert replayed2["kind"] == "trace"
                assert replayed2["event"]["id"] == "evt-2"
                assert replayed2["event"]["toolName"] == "search"


# ════════════════════════════════════════════════════════════════════
# Trace Forwarding: Agent → Relay → Viewer
# ════════════════════════════════════════════════════════════════════


class TestTraceForwarding:
    def test_agent_trace_forwarded_to_viewer(self, client):
        """Core flow: agent sends trace, viewer receives it in real-time."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                # Agent sends a trace
                trace = {
                    "kind": "trace",
                    "event": {
                        "id": "evt-live-1",
                        "sessionId": "test-session",
                        "timestamp": "2025-01-01T00:00:10Z",
                        "type": "agent_message",
                        "role": "assistant",
                        "content": "I'm thinking about the problem",
                    },
                }
                agent.send_json(trace)

                # Viewer should receive it
                received = viewer.receive_json()
                assert received["kind"] == "trace"
                assert received["event"]["id"] == "evt-live-1"
                assert received["event"]["content"] == "I'm thinking about the problem"

    def test_multiple_trace_types_forwarded(self, client):
        """All trace event types should be forwarded correctly."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                # Send different trace types
                traces = [
                    {"type": "status_change", "status": "thinking", "detail": "Planning"},
                    {"type": "tool_call", "toolName": "bash", "toolInput": {"cmd": "ls"}, "status": "started"},
                    {"type": "tool_result", "toolCallId": "tc-1", "output": "file.txt", "isError": False},
                    {"type": "stream_delta", "delta": "partial "},
                    {"type": "error", "code": "TIMEOUT", "message": "Request timed out"},
                ]

                for i, event_fields in enumerate(traces):
                    envelope = {
                        "kind": "trace",
                        "event": {
                            "id": f"evt-{i}",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            **event_fields,
                        },
                    }
                    agent.send_json(envelope)

                    received = viewer.receive_json()
                    assert received["kind"] == "trace"
                    assert received["event"]["type"] == event_fields["type"]

    def test_trace_forwarded_to_multiple_viewers(self, client):
        """Traces should be broadcast to all connected viewers."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer1:
                subscribe_viewer(viewer1, "test-session", secret)

                with client.websocket_connect("/ws") as viewer2:
                    subscribe_viewer(viewer2, "test-session", secret)

                    trace = {
                        "kind": "trace",
                        "event": {
                            "id": "evt-broadcast",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            "type": "agent_message",
                            "role": "assistant",
                            "content": "Broadcast message",
                        },
                    }
                    agent.send_json(trace)

                    # Both viewers receive it
                    r1 = viewer1.receive_json()
                    r2 = viewer2.receive_json()
                    assert r1["event"]["id"] == "evt-broadcast"
                    assert r2["event"]["id"] == "evt-broadcast"


# ════════════════════════════════════════════════════════════════════
# Command Forwarding: Viewer → Relay → Agent
# ════════════════════════════════════════════════════════════════════


class TestCommandForwarding:
    def test_inject_message_forwarded_to_agent(self, client):
        """Core flow: viewer sends inject_message, agent receives it."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                # Viewer sends a command
                viewer.send_json(
                    {
                        "kind": "command",
                        "command": {
                            "id": "cmd-1",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            "type": "inject_message",
                            "content": "Please search for Python docs",
                            "role": "user",
                        },
                    }
                )

                # Agent should receive the command
                received = agent.receive_json()
                assert received["kind"] == "command"
                assert received["command"]["type"] == "inject_message"
                assert received["command"]["content"] == "Please search for Python docs"
                assert received["command"]["id"] == "cmd-1"

    def test_cancel_command_forwarded_to_agent(self, client):
        """Viewer sends cancel, agent receives it."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                viewer.send_json(
                    {
                        "kind": "command",
                        "command": {
                            "id": "cmd-cancel",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            "type": "cancel",
                            "reason": "Taking too long",
                        },
                    }
                )

                received = agent.receive_json()
                assert received["command"]["type"] == "cancel"
                assert received["command"]["reason"] == "Taking too long"

    def test_approve_tool_forwarded_to_agent(self, client):
        """Viewer approves a tool call, agent receives it."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                viewer.send_json(
                    {
                        "kind": "command",
                        "command": {
                            "id": "cmd-approve",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            "type": "approve_tool",
                            "toolCallId": "tc-42",
                        },
                    }
                )

                received = agent.receive_json()
                assert received["command"]["type"] == "approve_tool"
                assert received["command"]["toolCallId"] == "tc-42"

    def test_deny_tool_forwarded_to_agent(self, client):
        """Viewer denies a tool call, agent receives it with reason."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                viewer.send_json(
                    {
                        "kind": "command",
                        "command": {
                            "id": "cmd-deny",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            "type": "deny_tool",
                            "toolCallId": "tc-99",
                            "reason": "Too dangerous",
                        },
                    }
                )

                received = agent.receive_json()
                assert received["command"]["type"] == "deny_tool"
                assert received["command"]["toolCallId"] == "tc-99"
                assert received["command"]["reason"] == "Too dangerous"

    def test_unknown_command_type_rejected(self, client):
        """Relay rejects commands with unknown types."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                viewer.send_json(
                    {
                        "kind": "command",
                        "command": {
                            "id": "cmd-bad",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            "type": "execute_shell",
                            "content": "rm -rf /",
                        },
                    }
                )

                resp = viewer.receive_json()
                assert "error" in resp
                assert "unknown command type" in resp["error"]


# ════════════════════════════════════════════════════════════════════
# Bidirectional Round-Trip
# ════════════════════════════════════════════════════════════════════


class TestBidirectionalRoundTrip:
    def test_full_conversation_round_trip(self, client):
        """
        Full round-trip:
        1. Agent registers
        2. Viewer subscribes
        3. Agent sends status → viewer receives
        4. Viewer sends command → agent receives
        5. Agent sends response → viewer receives
        """
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                # Step 1: Agent sends "thinking" status
                agent.send_json(
                    {
                        "kind": "trace",
                        "event": {
                            "id": "evt-thinking",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:01Z",
                            "type": "status_change",
                            "status": "waiting_for_input",
                            "detail": "What should I do?",
                        },
                    }
                )
                r1 = viewer.receive_json()
                assert r1["event"]["status"] == "waiting_for_input"

                # Step 2: Viewer injects a message
                viewer.send_json(
                    {
                        "kind": "command",
                        "command": {
                            "id": "cmd-user-input",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:02Z",
                            "type": "inject_message",
                            "content": "Search for FastAPI docs",
                        },
                    }
                )
                r2 = agent.receive_json()
                assert r2["command"]["content"] == "Search for FastAPI docs"

                # Step 3: Agent executes tool call
                agent.send_json(
                    {
                        "kind": "trace",
                        "event": {
                            "id": "evt-tool",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:03Z",
                            "type": "tool_call",
                            "toolName": "web_search",
                            "toolInput": {"query": "FastAPI documentation"},
                            "status": "started",
                        },
                    }
                )
                r3 = viewer.receive_json()
                assert r3["event"]["toolName"] == "web_search"

                # Step 4: Agent sends tool result
                agent.send_json(
                    {
                        "kind": "trace",
                        "event": {
                            "id": "evt-result",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:04Z",
                            "type": "tool_result",
                            "toolCallId": "evt-tool",
                            "output": "Found: https://fastapi.tiangolo.com",
                            "isError": False,
                        },
                    }
                )
                r4 = viewer.receive_json()
                assert r4["event"]["output"] == "Found: https://fastapi.tiangolo.com"

                # Step 5: Agent sends final message
                agent.send_json(
                    {
                        "kind": "trace",
                        "event": {
                            "id": "evt-reply",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:05Z",
                            "type": "agent_message",
                            "role": "assistant",
                            "content": "Here are the FastAPI docs: https://fastapi.tiangolo.com",
                        },
                    }
                )
                r5 = viewer.receive_json()
                assert "FastAPI docs" in r5["event"]["content"]


# ════════════════════════════════════════════════════════════════════
# Role Enforcement
# ════════════════════════════════════════════════════════════════════


class TestRoleEnforcement:
    def test_viewer_cannot_send_traces(self, client):
        """Viewers should not be able to send trace events."""
        with client.websocket_connect("/ws") as agent:
            secret = register_agent(agent)

            with client.websocket_connect("/ws") as viewer:
                subscribe_viewer(viewer, "test-session", secret)

                # Viewer tries to send a trace (should be rejected)
                viewer.send_json(
                    {
                        "kind": "trace",
                        "event": {
                            "id": "evt-imposter",
                            "sessionId": "test-session",
                            "timestamp": "2025-01-01T00:00:00Z",
                            "type": "agent_message",
                            "role": "assistant",
                            "content": "I am not the agent",
                        },
                    }
                )
                resp = viewer.receive_json()
                assert "error" in resp
                assert "not authorized" in resp["error"]

    def test_unauthenticated_cannot_send_commands(self, client):
        """An unauthenticated WebSocket should not be able to send commands."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "kind": "command",
                    "command": {
                        "id": "cmd-sneaky",
                        "sessionId": "test-session",
                        "timestamp": "2025-01-01T00:00:00Z",
                        "type": "inject_message",
                        "content": "Sneaky command",
                    },
                }
            )
            resp = ws.receive_json()
            assert "error" in resp

    def test_unauthenticated_cannot_send_traces(self, client):
        """An unauthenticated WebSocket should not be able to send traces."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "kind": "trace",
                    "event": {
                        "id": "evt-sneaky",
                        "sessionId": "test-session",
                        "timestamp": "2025-01-01T00:00:00Z",
                        "type": "agent_message",
                        "role": "assistant",
                        "content": "Sneaky trace",
                    },
                }
            )
            resp = ws.receive_json()
            assert "error" in resp


# ════════════════════════════════════════════════════════════════════
# Ping/Pong
# ════════════════════════════════════════════════════════════════════


class TestPingPong:
    def test_ping_returns_pong(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"kind": "ping"})
            resp = ws.receive_json()
            assert resp == {"kind": "pong"}


# ════════════════════════════════════════════════════════════════════
# Error Handling
# ════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    def test_invalid_json_returns_error(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.send_text("not json{{{")
            resp = ws.receive_json()
            assert "error" in resp
            assert "invalid JSON" in resp["error"]

    def test_unknown_kind_returns_error(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"kind": "hack"})
            resp = ws.receive_json()
            assert "error" in resp
