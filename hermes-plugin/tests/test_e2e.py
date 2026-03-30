"""Tests for E2E encryption in the Hermes ARC plugin."""

from __future__ import annotations

import base64
import json
import os

import pytest


@pytest.fixture
def relay():
    """Create a fresh ArcRelay with E2E enabled."""
    from hermes_plugin_path import ArcRelay

    r = ArcRelay()
    r.e2e_enabled = True
    r.session_id = "test-session-123"
    return r


class TestE2EKeyDerivation:
    """Test HKDF key derivation matches expected behavior."""

    def test_derive_key_produces_32_bytes(self, relay):
        key = relay._derive_e2e_key("my-session-pin", "session-123")
        assert isinstance(key, bytes)
        assert len(key) == 32  # AES-256

    def test_same_inputs_produce_same_key(self, relay):
        key1 = relay._derive_e2e_key("pin-abc", "session-1")
        key2 = relay._derive_e2e_key("pin-abc", "session-1")
        assert key1 == key2

    def test_different_pins_produce_different_keys(self, relay):
        key1 = relay._derive_e2e_key("pin-abc", "session-1")
        key2 = relay._derive_e2e_key("pin-xyz", "session-1")
        assert key1 != key2

    def test_different_sessions_produce_different_keys(self, relay):
        key1 = relay._derive_e2e_key("pin-abc", "session-1")
        key2 = relay._derive_e2e_key("pin-abc", "session-2")
        assert key1 != key2


class TestE2EEncryptDecrypt:
    """Test AES-256-GCM encrypt/decrypt round-trips."""

    def _setup_key(self, relay):
        relay._e2e_key = relay._derive_e2e_key("test-pin", relay.session_id)

    def test_encrypt_produces_ciphertext_and_nonce(self, relay):
        self._setup_key(relay)
        event = {"type": "agent_message", "content": "hello world"}
        encrypted = relay._encrypt_event(event)

        assert "ciphertext" in encrypted
        assert "nonce" in encrypted
        assert isinstance(encrypted["ciphertext"], str)
        assert isinstance(encrypted["nonce"], str)
        # Should be base64
        base64.b64decode(encrypted["ciphertext"])
        base64.b64decode(encrypted["nonce"])

    def test_encrypt_decrypt_roundtrip(self, relay):
        self._setup_key(relay)
        event = {"type": "tool_call", "toolName": "terminal", "status": "started"}
        encrypted = relay._encrypt_event(event)
        decrypted = relay._decrypt_payload(encrypted)

        assert decrypted == event

    def test_different_encryptions_produce_different_ciphertext(self, relay):
        self._setup_key(relay)
        event = {"type": "test", "data": "same content"}
        enc1 = relay._encrypt_event(event)
        enc2 = relay._encrypt_event(event)

        # Random nonce ensures different ciphertext each time
        assert enc1["ciphertext"] != enc2["ciphertext"]
        assert enc1["nonce"] != enc2["nonce"]

    def test_decrypt_with_wrong_key_fails_gracefully(self, relay):
        self._setup_key(relay)
        event = {"type": "test", "data": "secret"}
        encrypted = relay._encrypt_event(event)

        # Change the key
        relay._e2e_key = relay._derive_e2e_key("wrong-pin", relay.session_id)
        # Should return raw payload on failure (graceful fallback)
        result = relay._decrypt_payload(encrypted)
        assert result == encrypted  # Fallback returns original

    def test_decrypt_with_wrong_session_id_fails(self, relay):
        """With real AES-GCM, wrong session ID (AAD) causes decrypt failure.
        With the test mock, AAD isn't enforced, so we skip this test
        when running without the real cryptography package."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            # Check if this is the real AESGCM or our test mock
            if not hasattr(AESGCM, "__module__") or "cryptography" not in getattr(AESGCM, "__module__", ""):
                pytest.skip("Mock crypto doesn't enforce AAD")
        except Exception:
            pytest.skip("cryptography not available")

        self._setup_key(relay)
        event = {"type": "test", "data": "secret"}
        encrypted = relay._encrypt_event(event)

        relay.session_id = "different-session"
        result = relay._decrypt_payload(encrypted)
        assert result == encrypted  # AAD mismatch → decrypt fails → fallback

    def test_no_key_returns_plaintext(self, relay):
        relay._e2e_key = None
        event = {"type": "test", "data": "not encrypted"}
        result = relay._encrypt_event(event)
        assert result == event  # No encryption when key is None


class TestE2ESendTrace:
    """Test that send_trace encrypts when E2E is enabled."""

    def _setup_connected(self, relay):
        """Set up a relay that looks connected with a mock WS."""
        relay._e2e_key = relay._derive_e2e_key("test-pin", relay.session_id)
        relay.connected = True

        class MockWS:
            def __init__(self):
                self.sent = []

            def send(self, data):
                self.sent.append(data)

        ws = MockWS()
        relay.ws = ws
        return ws

    def test_send_trace_encrypts_when_e2e(self, relay):
        ws = self._setup_connected(relay)

        relay.send_trace({"type": "agent_message", "content": "hello"})

        assert len(ws.sent) == 1
        envelope = json.loads(ws.sent[0])
        assert envelope["kind"] == "trace"
        assert envelope["encrypted"] is True
        assert "ciphertext" in envelope["event"]
        assert "nonce" in envelope["event"]
        # Should NOT have plaintext content
        assert "content" not in envelope["event"]

    def test_send_trace_plaintext_when_no_e2e(self, relay):
        relay.e2e_enabled = False
        relay._e2e_key = None
        relay.connected = True

        class MockWS:
            def __init__(self):
                self.sent = []

            def send(self, data):
                self.sent.append(data)

        relay.ws = MockWS()

        relay.send_trace({"type": "agent_message", "content": "hello"})

        envelope = json.loads(relay.ws.sent[0])
        assert envelope["kind"] == "trace"
        assert "encrypted" not in envelope
        assert envelope["event"]["content"] == "hello"
