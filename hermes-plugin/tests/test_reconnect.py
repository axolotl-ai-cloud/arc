"""Tests for the ARC relay plugin reconnect logic."""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

import pytest


class FakeWebSocket:
    """Mock WebSocket that can simulate connect/disconnect/reconnect."""

    def __init__(self):
        self.connected = False
        self.sent: list[str] = []
        self._recv_queue: list[str] = []
        self._should_fail_connect = False
        self._should_fail_after = None  # Disconnect after N recv calls
        self._recv_count = 0
        self._closed = False
        self._timeout = None

    def connect(self, url, timeout=None):
        if self._should_fail_connect:
            raise ConnectionRefusedError("Connection refused")
        self.connected = True
        self._closed = False
        self._recv_count = 0

    def send(self, data):
        if self._closed:
            raise BrokenPipeError("WebSocket is closed")
        self.sent.append(data)

    def recv(self):
        if self._closed:
            raise ConnectionError("WebSocket is closed")
        if self._should_fail_after is not None and self._recv_count >= self._should_fail_after:
            self._closed = True
            raise ConnectionError("Connection lost")
        self._recv_count += 1
        if self._recv_queue:
            return self._recv_queue.pop(0)
        # Simulate timeout
        import websocket

        raise websocket.WebSocketTimeoutException("timeout")

    def settimeout(self, t):
        self._timeout = t

    def close(self):
        self._closed = True
        self.connected = False

    def queue_response(self, data: dict):
        self._recv_queue.append(json.dumps(data))


@pytest.fixture
def mock_websocket():
    """Provide a controllable fake WebSocket."""
    return FakeWebSocket()


def _make_registration_response(pin="test-viewer-pin-12345"):
    return {"kind": "registered", "sessionSecret": pin}


class TestArcRelayReconnect:
    """Test the ArcRelay._run reconnect loop."""

    def _create_relay(self):
        """Import and create a fresh ArcRelay instance."""
        # Import here to avoid module-level side effects
        import sys

        # Remove cached module to get fresh state
        for mod_name in list(sys.modules):
            if "arc_remote_control" in mod_name:
                del sys.modules[mod_name]

        # We need to test the ArcRelay class directly
        from hermes_plugin_path import ArcRelay

        return ArcRelay()

    def test_successful_connect(self, mock_websocket):
        """Test that a successful connection sets connected=True."""
        from hermes_plugin_path import ArcRelay

        relay = ArcRelay()

        connect_count = 0
        original_connect = mock_websocket.connect

        def connect_once(url, timeout=None):
            nonlocal connect_count
            connect_count += 1
            if connect_count > 1:
                relay._stop.set()
                raise ConnectionRefusedError("Stopping")
            original_connect(url, timeout=timeout)
            mock_websocket.queue_response(_make_registration_response())
            mock_websocket._should_fail_after = 2
            mock_websocket._recv_count = 0

        mock_websocket.connect = connect_once

        with patch("websocket.WebSocket", return_value=mock_websocket):
            with patch("time.sleep"):
                relay._stop = threading.Event()
                session_info = {
                    "sessionId": "test-123",
                    "agentFramework": "hermes",
                    "agentName": "test",
                    "startedAt": "2024-01-01T00:00:00Z",
                }
                relay._run("ws://localhost:8600/ws", "test-passphrase", session_info)

        # Should have registered
        register_msgs = [json.loads(s) for s in mock_websocket.sent if "register" in s]
        assert any(m.get("kind") == "register" for m in register_msgs)
        assert connect_count >= 1

    def test_reconnect_after_disconnect(self, mock_websocket):
        """Test that the relay reconnects after a WebSocket disconnect."""
        from hermes_plugin_path import ArcRelay

        relay = ArcRelay()

        connect_count = 0
        original_connect = mock_websocket.connect

        def counting_connect(url, timeout=None):
            nonlocal connect_count
            connect_count += 1
            if connect_count > 2:
                # Stop after 2 connects
                relay._stop.set()
                raise ConnectionRefusedError("Stopping test")
            original_connect(url, timeout=timeout)
            # Queue registration response for each connect
            mock_websocket.queue_response(_make_registration_response())
            # Disconnect after 1 recv to trigger reconnect
            mock_websocket._should_fail_after = 1
            mock_websocket._recv_count = 0
            mock_websocket._closed = False

        mock_websocket.connect = counting_connect

        with patch("websocket.WebSocket", return_value=mock_websocket):
            with patch("time.sleep"):  # Don't actually sleep in tests
                relay._stop = threading.Event()
                session_info = {
                    "sessionId": "reconnect-test",
                    "agentFramework": "hermes",
                    "agentName": "test",
                    "startedAt": "2024-01-01T00:00:00Z",
                }

                relay._run("ws://localhost:8600/ws", "test-passphrase", session_info)

        # Should have connected twice (initial + 1 reconnect)
        assert connect_count >= 2

    def test_reconnect_preserves_session_id(self, mock_websocket):
        """Test that reconnect uses the same session ID."""
        from hermes_plugin_path import ArcRelay

        relay = ArcRelay()

        registrations = []
        connect_count = 0
        original_connect = mock_websocket.connect
        original_send = mock_websocket.send

        def tracking_send(data):
            parsed = json.loads(data)
            if parsed.get("kind") == "register":
                registrations.append(parsed)
            original_send(data)

        def counting_connect(url, timeout=None):
            nonlocal connect_count
            connect_count += 1
            if connect_count > 3:
                relay._stop.set()
                raise ConnectionRefusedError("Stopping test")
            original_connect(url, timeout=timeout)
            mock_websocket.queue_response(_make_registration_response())
            mock_websocket._should_fail_after = 1
            mock_websocket._recv_count = 0
            mock_websocket._closed = False
            mock_websocket.sent = []

        mock_websocket.connect = counting_connect
        mock_websocket.send = tracking_send

        with patch("websocket.WebSocket", return_value=mock_websocket):
            with patch("time.sleep"):
                relay._stop = threading.Event()
                session_info = {
                    "sessionId": "persist-id-test",
                    "agentFramework": "hermes",
                    "agentName": "test",
                    "startedAt": "2024-01-01T00:00:00Z",
                }

                relay._run("ws://localhost:8600/ws", "test-passphrase", session_info)

        # All registrations should use the same session ID
        assert len(registrations) >= 2
        for reg in registrations:
            assert reg["session"]["sessionId"] == "persist-id-test"

    def test_gives_up_after_max_attempts(self, mock_websocket):
        """Test that reconnect gives up after max attempts."""
        from hermes_plugin_path import ArcRelay

        relay = ArcRelay()

        connect_count = 0

        def always_fail(url, timeout=None):
            nonlocal connect_count
            connect_count += 1
            raise ConnectionRefusedError("Connection refused")

        mock_websocket.connect = always_fail

        with patch("websocket.WebSocket", return_value=mock_websocket):
            with patch("time.sleep"):
                relay._stop = threading.Event()
                session_info = {
                    "sessionId": "give-up-test",
                    "agentFramework": "hermes",
                    "agentName": "test",
                    "startedAt": "2024-01-01T00:00:00Z",
                }

                relay._run("ws://localhost:8600/ws", "test-passphrase", session_info)

        # Should have tried 21 times (1 initial + 20 retries)
        assert connect_count == 21

    def test_stop_event_halts_reconnect(self, mock_websocket):
        """Test that setting _stop prevents further reconnect attempts."""
        from hermes_plugin_path import ArcRelay

        relay = ArcRelay()

        connect_count = 0

        def fail_then_stop(url, timeout=None):
            nonlocal connect_count
            connect_count += 1
            if connect_count >= 2:
                relay._stop.set()
            raise ConnectionRefusedError("Connection refused")

        mock_websocket.connect = fail_then_stop

        with patch("websocket.WebSocket", return_value=mock_websocket):
            with patch("time.sleep"):
                relay._stop = threading.Event()
                session_info = {
                    "sessionId": "stop-test",
                    "agentFramework": "hermes",
                    "agentName": "test",
                    "startedAt": "2024-01-01T00:00:00Z",
                }

                relay._run("ws://localhost:8600/ws", "test-passphrase", session_info)

        # Should have stopped after ~2 attempts
        assert connect_count <= 3

    def test_registration_failure_stops_retry(self, mock_websocket):
        """Test that a registration error (bad token) doesn't retry."""
        from hermes_plugin_path import ArcRelay

        relay = ArcRelay()

        mock_websocket.queue_response({"error": "invalid agent token"})

        connect_count = 0
        original_connect = mock_websocket.connect

        def counting_connect(url, timeout=None):
            nonlocal connect_count
            connect_count += 1
            original_connect(url, timeout=timeout)

        mock_websocket.connect = counting_connect

        with patch("websocket.WebSocket", return_value=mock_websocket):
            with patch("time.sleep"):
                relay._stop = threading.Event()
                session_info = {
                    "sessionId": "bad-token-test",
                    "agentFramework": "hermes",
                    "agentName": "test",
                    "startedAt": "2024-01-01T00:00:00Z",
                }

                relay._run("ws://localhost:8600/ws", "bad-token", session_info)

        # Should NOT retry — registration failure is permanent
        assert connect_count == 1
