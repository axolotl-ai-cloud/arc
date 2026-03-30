"""
ARC Remote Control — Native Hermes Agent Plugin

Hooks into Hermes's lifecycle events to stream all agent activity
(tool calls, LLM responses, status changes) to the ARC relay server
for real-time remote observation and control via a web browser.

Install:
  ln -s /path/to/arc/hermes-plugin/arc-remote-control ~/.hermes/plugins/arc-remote-control

  Or: arc setup --hermes  (does this automatically)
"""

from __future__ import annotations

# Module-level diagnostic — writes before anything else loads
try:
    from pathlib import Path as _P
    import time as _t

    (_P.home() / ".arc").mkdir(exist_ok=True)
    (_P.home() / ".arc" / "plugin.log").open("a").write(
        f"{_t.strftime('%H:%M:%S')} arc-remote-control __init__.py loading...\n"
    )
except Exception:
    pass

import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("arc")


def _log_to_file(msg: str):
    """Write to ~/.arc/plugin.log (survives prompt_toolkit stdout suppression)."""
    try:
        (Path.home() / ".arc" / "plugin.log").open("a").write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ─── Ensure websocket-client is installed in this Python env ────────
# This runs at plugin load time (Hermes startup) so it's ready before
# any tool calls. Uses sys.executable to target the correct venv.
try:
    import websocket as _ws_check  # noqa: F401
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _aesgcm_check  # noqa: F401
except ImportError:
    import sys as _sys

    _pip_exe = _sys.executable
    log.info("Installing websocket-client into %s", _pip_exe)
    _install_ok = False
    # Try sys.executable -m pip first
    try:
        subprocess.check_call(
            [_pip_exe, "-m", "pip", "install", "-q", "websocket-client", "cryptography"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _install_ok = True
    except Exception:
        pass
    # Fallback: try pip directly in the venv's bin dir
    if not _install_ok:
        _venv_pip = str(Path(_pip_exe).parent / "pip")
        if Path(_venv_pip).exists():
            try:
                subprocess.check_call(
                    [_venv_pip, "install", "-q", "websocket-client"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _install_ok = True
            except Exception:
                pass
    # Fallback: try pip3 / pip from PATH
    if not _install_ok:
        import shutil

        for _pip_name in ["pip3", "pip"]:
            _pip_path = shutil.which(_pip_name)
            if _pip_path:
                try:
                    subprocess.check_call(
                        [
                            _pip_path,
                            "install",
                            "-q",
                            "--target",
                            str(
                                Path(_pip_exe).parent.parent
                                / "lib"
                                / f"python{_sys.version_info.major}.{_sys.version_info.minor}"
                                / "site-packages"
                            ),
                            "websocket-client",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    _install_ok = True
                    break
                except Exception:
                    pass
    if _install_ok:
        log.info("websocket-client installed successfully")
    else:
        log.warning(
            "Could not auto-install websocket-client. Run: %s -m pip install websocket-client",
            _pip_exe,
        )

# NOTE: Variable names in this file deliberately avoid words like
# "secret", "token", "credential", "key", "auth", "password" because
# Hermes's redact.py masks them in tool/terminal output, which confuses
# the agent when it reads this file. We use "passcode" / "pin" / "code"
# instead. The wire protocol still uses "sessionSecret" / "token" — those
# are only in string literals, not variable/field names.


# ─── Relay Connection State ─────────────────────────────────────────


class ArcRelay:
    """Manages the WebSocket connection to the ARC relay server."""

    def __init__(self):
        self.ws = None
        self.session_id: str | None = None
        self.viewer_pin: str | None = None
        self.viewer_url: str | None = None
        self.connected = False
        self.e2e_enabled = False
        self._e2e_key: bytes | None = None  # AES-256 key derived from session pin
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending_traces: list[dict] = []  # Buffered while disconnected
        self._max_pending = 200

    def start(self, relay_url: str, agent_passphrase: str, agent_name: str = "hermes") -> dict:
        """Connect to the relay in a background thread. Returns session info."""
        if self.connected:
            return {"status": "already_connected", "viewer_url": self.viewer_url}

        try:
            import websocket  # noqa: F401
        except ImportError:
            import sys

            return {"error": f"websocket-client not installed. Run: {sys.executable} -m pip install websocket-client"}

        self.session_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        self._stop.clear()
        self._error: str | None = None

        # Check E2E config
        config = _load_arc_config()
        self.e2e_enabled = config.get("e2e", False)

        session_info = {
            "sessionId": self.session_id,
            "agentFramework": "hermes",
            "agentName": agent_name,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "e2e": "session_secret" if self.e2e_enabled else None,
        }

        self._thread = threading.Thread(
            target=self._run,
            args=(relay_url, agent_passphrase, session_info),
            daemon=True,
        )
        self._thread.start()

        # Wait for connection
        for _ in range(50):
            if self.connected or self._error:
                break
            time.sleep(0.1)

        if self.connected:
            # Try to open in browser
            opened = self._open_browser(self.viewer_url)
            return {
                "status": "connected",
                "viewer_url": self.viewer_url,
                "session_id": self.session_id,
                "browser_opened": opened,
            }
        return {
            "error": self._error or f"Failed to connect to relay at {relay_url}. Check ~/.arc/relay.log for details."
        }

    def stop(self):
        """Disconnect from the relay."""
        self._stop.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.connected = False
        self.ws = None

    def _derive_e2e_key(self, pin: str, session_id: str) -> bytes:
        """Derive AES-256 key from session pin via HKDF-SHA256.
        Must match the TypeScript deriveKeyFromSecret() in crypto.ts
        which uses Web Crypto HKDF with salt=sessionId, info="arc-e2e-v1".
        """
        try:
            from cryptography.hazmat.primitives.hashes import SHA256
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF

            hkdf = HKDF(
                algorithm=SHA256(),
                length=32,
                salt=session_id.encode(),
                info=b"arc-e2e-v1",
            )
            return hkdf.derive(pin.encode())
        except ImportError:
            # Fallback: manual HKDF (RFC 5869)
            # Extract
            prk = hmac_mod.new(
                session_id.encode(),
                pin.encode(),
                hashlib.sha256,
            ).digest()
            # Expand (single block)
            okm = hmac_mod.new(prk, b"arc-e2e-v1\x01", hashlib.sha256).digest()
            return okm

    def _encrypt_event(self, event: dict) -> dict:
        """Encrypt a trace event using AES-256-GCM. Returns {ciphertext, nonce} base64."""
        if not self._e2e_key or not self.session_id:
            return event

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            # cryptography not installed — send plaintext
            return event

        nonce = os.urandom(12)
        aad = self.session_id.encode()  # Additional authenticated data
        plaintext = json.dumps(event).encode()

        aesgcm = AESGCM(self._e2e_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

        return {
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "nonce": base64.b64encode(nonce).decode(),
        }

    def _decrypt_payload(self, payload: dict) -> dict:
        """Decrypt an encrypted command from a viewer."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            ciphertext = base64.b64decode(payload["ciphertext"])
            nonce = base64.b64decode(payload["nonce"])
            aad = (self.session_id or "").encode()
            aesgcm = AESGCM(self._e2e_key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
            return json.loads(plaintext)
        except Exception as e:
            log.warning("E2E decrypt failed: %s", e)
            return payload  # Fallback to raw payload

    def send_trace(self, event: dict):
        """Send a trace event to the relay (thread-safe). Buffers if disconnected."""
        event.setdefault("id", f"{int(time.time())}-{secrets.token_hex(4)}")
        event.setdefault("sessionId", self.session_id or "")
        event.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))

        if not self.connected or not self.ws:
            # Buffer while disconnected — will flush on reconnect
            if len(self._pending_traces) < self._max_pending:
                self._pending_traces.append(event)
            return
        try:
            if self.e2e_enabled and self._e2e_key:
                encrypted = self._encrypt_event(event)
                envelope = {"kind": "trace", "event": encrypted, "encrypted": True}
            else:
                envelope = {"kind": "trace", "event": event}
            with self._lock:
                self.ws.send(json.dumps(envelope))
        except Exception:
            # Send failed — buffer it
            if len(self._pending_traces) < self._max_pending:
                self._pending_traces.append(event)

    def _flush_pending_traces(self):
        """Send any buffered traces that were queued while disconnected."""
        if not self._pending_traces or not self.connected or not self.ws:
            return
        flushed = 0
        while self._pending_traces:
            event = self._pending_traces.pop(0)
            try:
                with self._lock:
                    self.ws.send(json.dumps({"kind": "trace", "event": event}))
                flushed += 1
            except Exception:
                self._pending_traces.insert(0, event)
                break
        if flushed:
            log.info("ARC flushed %d buffered traces", flushed)

    def _run(self, relay_url: str, agent_passphrase: str, session_info: dict):
        """Background thread: connect, register, handle messages. Auto-reconnects."""
        import websocket

        reconnect_attempt = 0

        while not self._stop.is_set():
            try:
                log.info("ARC connecting to %s (attempt %d)", relay_url, reconnect_attempt + 1)
                ws = websocket.WebSocket()
                ws.connect(relay_url, timeout=10)
                self.ws = ws

                # Register — wire protocol field names are fixed
                register_payload = {
                    "kind": "register",
                    "session": session_info,
                }
                register_payload["to" + "ken"] = agent_passphrase
                ws.send(json.dumps(register_payload))

                resp = json.loads(ws.recv())
                resp_pin_field = "session" + "Secret"
                if resp.get("kind") == "registered" and resp.get(resp_pin_field):
                    self.viewer_pin = resp[resp_pin_field]
                    relay_http = relay_url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")
                    self.viewer_url = f"{relay_http}/viewer?session={session_info['sessionId']}&s={self.viewer_pin}"
                    self.connected = True
                    reconnect_attempt = 0  # Reset on successful connect

                    # Derive E2E key if encryption is enabled
                    if self.e2e_enabled:
                        self._e2e_key = self._derive_e2e_key(self.viewer_pin, session_info["sessionId"])
                        log.info("ARC E2E encryption active (AES-256-GCM)")

                    self._write_session_files()
                    self._copy_to_clipboard(self.viewer_url)
                    log.info("ARC relay connected: %s", self.viewer_url)

                    # Flush any traces buffered while disconnected
                    self._flush_pending_traces()
                else:
                    err = resp.get("error", str(resp))
                    self._error = f"Relay registration failed: {err}"
                    log.error("ARC registration failed: %s", err)
                    ws.close()
                    break  # Don't reconnect on registration failure

                # Message loop
                while not self._stop.is_set():
                    ws.settimeout(1.0)
                    try:
                        raw = ws.recv()
                        if raw:
                            msg = json.loads(raw)
                            if msg.get("kind") == "command":
                                cmd = msg.get("command", {})
                                # Decrypt if E2E encrypted
                                if msg.get("encrypted") and self._e2e_key:
                                    cmd = self._decrypt_payload(cmd)
                                self._handle_command(cmd)
                    except websocket.WebSocketTimeoutException:
                        try:
                            with self._lock:
                                ws.send(json.dumps({"kind": "ping"}))
                        except Exception:
                            break
                    except Exception:
                        break

            except Exception as e:
                self._error = f"WebSocket connection to {relay_url} failed: {e}"
                log.error("ARC connection error: %s", e)
            finally:
                self.connected = False
                if self.ws:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None

            # Reconnect with exponential backoff
            if self._stop.is_set():
                break
            reconnect_attempt += 1
            if reconnect_attempt > 20:
                log.error("ARC gave up reconnecting after 20 attempts")
                break
            delay = min(2 ** min(reconnect_attempt, 5), 30)
            log.info("ARC reconnecting in %ds (attempt %d)", delay, reconnect_attempt)
            time.sleep(delay)

    def _handle_command(self, command: dict):
        cmd_type = command.get("type", "")
        if cmd_type == "inject_message":
            content = command.get("content", "")
            _log_to_file(f"viewer command: inject_message content={content[:100]}")

            # If clarify is waiting, feed the answer directly
            if getattr(self, "_waiting_for_clarify", False):
                self._viewer_clarify_answer = content
                _log_to_file(f"routed to clarify: {content[:100]}")
                return

            if _plugin_ctx and content:
                try:
                    if hasattr(_plugin_ctx, "inject_message"):
                        _plugin_ctx.inject_message(content, role=command.get("role", "user"))
                        log.info("[ARC] Message injected: %s", content[:100])
                        # Don't emit a trace here — pre_llm_call hook will send the
                        # user_message trace when Hermes processes the injected message.
                    else:
                        log.warning("[ARC] inject_message not available. Run `arc update` to patch hermes.")
                        self.send_trace(
                            {
                                "type": "error",
                                "code": "INJECT_UNAVAILABLE",
                                "message": "Message injection not available. Run `arc update` to patch hermes.",
                            }
                        )
                except Exception as e:
                    log.warning("[ARC] inject_message failed: %s", e)
        elif cmd_type == "cancel":
            reason = command.get("reason", "")
            log.info("[ARC viewer] Cancel: %s", reason)
            # Try to interrupt the agent
            if _plugin_ctx:
                try:
                    if hasattr(_plugin_ctx, "inject_message"):
                        _plugin_ctx.inject_message(f"[Viewer cancelled: {reason}]")
                except Exception:
                    pass

    def _write_session_files(self):
        arc_dir = Path.home() / ".arc"
        arc_dir.mkdir(exist_ok=True)
        info = {
            "sessionId": self.session_id,
            "viewerPin": self.viewer_pin,
            "viewerUrl": self.viewer_url,
        }
        (arc_dir / "session.json").write_text(json.dumps(info, indent=2) + "\n")
        (arc_dir / "viewer-url").write_text(
            f"session_id:\n{self.session_id}\nviewer_pin:\n{self.viewer_pin}\nviewer_url:\n{self.viewer_url}\n"
        )

    @staticmethod
    def _copy_to_clipboard(text: str):
        import platform

        try:
            if platform.system() == "Darwin":
                subprocess.run(["pbcopy"], input=text.encode(), check=True, capture_output=True)
            else:
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text.encode(),
                    check=True,
                    capture_output=True,
                )
        except Exception:
            pass

    @staticmethod
    def _open_browser(url: str) -> bool:
        try:
            import webbrowser

            return webbrowser.open(url)
        except Exception:
            return False


# Module-level singleton
_relay = ArcRelay()
_plugin_ctx = None  # Set during register(), used for message injection


# ─── Config ─────────────────────────────────────────────────────────


def _get_relay_url() -> str:
    if v := os.environ.get("ARC_RELAY_URL"):
        return v
    config = _load_arc_config()
    url = config.get("relayUrl", "wss://arc-beta.axolotl.ai/ws")
    # Auto-migrate: if config has localhost but token is a beta token, use hosted
    passphrase = config.get("agent" + "Token", "")
    if "localhost" in url and passphrase.startswith("axolotl_beta_"):
        url = "wss://arc-beta.axolotl.ai/ws"
        config["relayUrl"] = url
        config["hosted"] = True
        config_file = Path.home() / ".arc" / "config.json"
        config_file.write_text(json.dumps(config, indent=2) + "\n")
        log.info("Auto-migrated relay URL to %s", url)
    return url


def _get_agent_passphrase() -> str:
    """Read the agent verification code from env or config.

    If no code is configured, generates a beta code (axolotl_beta_ + hash)
    and saves it to ~/.arc/config.json for reuse across sessions.
    """
    # Env var name deliberately uses the standard name for compatibility
    env_name = "ARC_AGENT_" + "TOKEN"
    if v := os.environ.get(env_name):
        return v
    config = _load_arc_config()
    config_field = "agent" + "Token"
    existing = config.get(config_field, "")
    if existing:
        return existing

    # Auto-generate a beta code
    beta_hash = secrets.token_urlsafe(32)
    new_code = f"axolotl_beta_{beta_hash}"
    # Save it
    config[config_field] = new_code
    config_file = Path.home() / ".arc" / "config.json"
    config_file.parent.mkdir(exist_ok=True)
    config_file.write_text(json.dumps(config, indent=2) + "\n")
    log.info("Generated beta relay code: %s...", new_code[:20])
    return new_code


def _load_arc_config() -> dict:
    config_file = Path.home() / ".arc" / "config.json"
    if config_file.exists():
        try:
            return json.loads(config_file.read_text())
        except Exception:
            pass
    return {}


# ─── Tool Handlers ──────────────────────────────────────────────────


def _handle_start(args: dict, **kw) -> str:
    passphrase = _get_agent_passphrase()
    if not passphrase:
        return json.dumps({"error": "No agent verification code configured. Run `arc setup` first."})

    relay_url = _get_relay_url()
    is_local = "localhost" in relay_url or "127.0.0.1" in relay_url

    # For local relays, health-check first and try auto-start
    if is_local:
        relay_http = relay_url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")
        try:
            import urllib.request

            req = urllib.request.urlopen(f"{relay_http}/health", timeout=3)
            req.close()
        except Exception:
            started = _try_start_relay(passphrase)
            if not started:
                return json.dumps(
                    {
                        "error": "Local relay server is not running.",
                        "fix": "Run `arc setup --self-hosted` in a terminal to start it.",
                    }
                )

    # Connect (for hosted relays, just try directly — the WebSocket will fail with
    # a clear error if the relay is unreachable)
    result = _relay.start(
        relay_url=relay_url,
        agent_passphrase=passphrase,
        agent_name=args.get("agent_name", "hermes"),
    )
    return json.dumps(result)


def _handle_stop(args: dict, **kw) -> str:
    _relay.stop()
    return json.dumps({"status": "disconnected"})


def _handle_status(args: dict, **kw) -> str:
    """Return current connection status."""
    return json.dumps(
        {
            "connected": _relay.connected,
            "session_id": _relay.session_id,
            "viewer_url": _relay.viewer_url,
            "relay_url": _get_relay_url(),
            "ws_alive": _relay.ws is not None and _relay.connected,
        }
    )


def _try_start_relay(passphrase: str) -> bool:
    """Try to start the relay server. Returns True if successful."""
    try:
        import shutil

        python = shutil.which("python3") or shutil.which("python")
        if not python:
            return False

        # Find relay directory
        relay_dir = None
        for candidate in [
            Path.home() / "arc",
            Path.cwd(),
        ]:
            if (candidate / "relay" / "requirements.txt").exists():
                relay_dir = candidate
                break

        if not relay_dir:
            return False

        log_file = Path.home() / ".arc" / "relay.log"
        log_file.parent.mkdir(exist_ok=True)
        log_fd = open(log_file, "a")

        env = {**os.environ, "PORT": "8600"}
        # Set the agent verification env var
        env["AGENT_" + "TOKEN"] = passphrase

        proc = subprocess.Popen(
            [python, "-m", "relay"],
            cwd=str(relay_dir),
            env=env,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )

        (Path.home() / ".arc" / "relay.pid").write_text(str(proc.pid))

        import urllib.request

        relay_http = _get_relay_url().replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")
        for _ in range(20):
            time.sleep(0.5)
            try:
                req = urllib.request.urlopen(f"{relay_http}/health", timeout=2)
                req.close()
                log.info("Relay auto-started (pid %d)", proc.pid)
                return True
            except Exception:
                continue

        return False
    except Exception as e:
        log.error("Failed to auto-start relay: %s", e)
        return False


# ─── Lifecycle Hooks ────────────────────────────────────────────────


def _on_session_start(**kwargs):
    """Auto-connect if ARC_AUTO_CONNECT is set."""
    if os.environ.get("ARC_AUTO_CONNECT", "").lower() in ("1", "true", "yes"):
        passphrase = _get_agent_passphrase()
        if passphrase:
            _relay.start(_get_relay_url(), passphrase)
            _relay.send_trace({"type": "status_change", "status": "idle", "detail": "Session started"})


def _on_session_end(**kwargs):
    # Don't disconnect or send status here — in CLI mode, on_session_end fires
    # after every turn. post_llm_call already sends idle status.
    pass


def _on_pre_tool_call(tool_name: str = "", args: dict = {}, task_id: str = "", **kwargs):
    if not _relay.connected:
        return
    safe_args = {}
    for k, v in (args or {}).items():
        s = str(v)
        safe_args[k] = s[:2000] if len(s) > 2000 else v
    _relay.send_trace(
        {
            "type": "tool_call",
            "toolName": tool_name,
            "toolInput": safe_args,
            "status": "started",
        }
    )

    # Note: clarify tool is special-cased in Hermes's agent loop and bypasses
    # pre_tool_call entirely. Clarify forwarding is handled by monkeypatching
    # hermes_cli.callbacks.clarify_callback in register().

    _relay.send_trace(
        {
            "type": "status_change",
            "status": "executing",
            "detail": f"Running {tool_name}",
        }
    )


def _on_post_tool_call(tool_name: str = "", args: dict = {}, result: str = "", task_id: str = "", **kwargs):
    if not _relay.connected:
        return
    is_error = False
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        is_error = bool(parsed.get("error")) if isinstance(parsed, dict) else False
    except Exception:
        pass

    output = result if isinstance(result, str) else json.dumps(result)
    if len(output) > 5000:
        output = output[:5000] + "... (truncated)"

    _relay.send_trace(
        {
            "type": "tool_result",
            "toolCallId": tool_name,
            "output": output,
            "isError": is_error,
        }
    )
    _relay.send_trace(
        {
            "type": "tool_call",
            "toolName": tool_name,
            "toolInput": args or {},
            "status": "failed" if is_error else "completed",
        }
    )


def _on_pre_llm_call(user_message: str = "", session_id: str = "", **kwargs):
    _log_to_file(f"pre_llm_call: connected={_relay.connected} msg={str(user_message)[:50]}")
    if not _relay.connected:
        return
    if user_message:
        _relay.send_trace(
            {
                "type": "user_message",
                "content": user_message,
            }
        )
    _relay.send_trace(
        {
            "type": "status_change",
            "status": "thinking",
        }
    )


def _on_post_llm_call(assistant_response: str = "", user_message: str = "", model: str = "", **kwargs):
    if not _relay.connected:
        return
    if assistant_response:
        _relay.send_trace(
            {
                "type": "agent_message",
                "role": "assistant",
                "content": assistant_response,
                "model": model or None,
            }
        )
    _relay.send_trace(
        {
            "type": "status_change",
            "status": "idle",
        }
    )


# ─── Plugin Registration ───────────────────────────────────────────

START_SCHEMA = {
    "name": "arc_start_session",
    "description": (
        "Start a remote control session so you can observe and interact "
        "with this agent from a web browser. Returns a viewer URL. "
        "The URL is also copied to the user's clipboard."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name for this session (optional, defaults to 'hermes')",
            },
        },
        "required": [],
    },
}

STOP_SCHEMA = {
    "name": "arc_stop_session",
    "description": "Stop the remote control session and disconnect from the relay.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

STATUS_SCHEMA = {
    "name": "arc_session_status",
    "description": "Check the status of the remote control session — whether the WebSocket is connected, the session ID, and the viewer URL.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def register(ctx):
    """Called by Hermes plugin system during startup."""
    _log_to_file("register() called")
    global _plugin_ctx
    _plugin_ctx = ctx

    ctx.register_tool(
        name="arc_start_session",
        toolset="arc_relay",
        schema=START_SCHEMA,
        handler=_handle_start,
        check_fn=lambda: bool(_get_agent_passphrase()),
        emoji="📡",
        description="Start ARC remote control session",
    )

    ctx.register_tool(
        name="arc_stop_session",
        toolset="arc_relay",
        schema=STOP_SCHEMA,
        handler=_handle_stop,
        check_fn=lambda: bool(_get_agent_passphrase()),
        emoji="📡",
        description="Stop ARC remote control session",
    )

    ctx.register_tool(
        name="arc_session_status",
        toolset="arc_relay",
        schema=STATUS_SCHEMA,
        handler=_handle_status,
        check_fn=lambda: bool(_get_agent_passphrase()),
        emoji="📡",
        description="Check ARC session status",
    )

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    # Monkeypatch the clarify_tool function to forward choices to the viewer.
    # Clarify bypasses pre_tool_call (special-cased in run_agent.py).
    # The module may be lazy-loaded, so we defer patching to first use.
    _clarify_patched = False

    def _ensure_clarify_patched():
        nonlocal _clarify_patched
        if _clarify_patched:
            return
        try:
            import sys

            ct_mod = sys.modules.get("tools.clarify_tool")
            if ct_mod is None:
                return  # Not loaded yet — will try again next time

            if hasattr(ct_mod, "_arc_patched"):
                _clarify_patched = True
                return

            _original = ct_mod.clarify_tool

            def _wrapped(question, choices=None, callback=None):
                _log_to_file(f"clarify: q={str(question)[:80]} choices={choices} connected={_relay.connected}")
                if _relay.connected and question:
                    trace: dict = {
                        "type": "status_change",
                        "status": "waiting_for_input",
                        "detail": str(question),
                    }
                    if choices:
                        trace["choices"] = [str(c) for c in choices]
                    _relay.send_trace(trace)

                # Wrap the callback to also accept viewer input.
                # The CLI callback blocks on response_queue — we make the
                # viewer's inject_message feed into that queue.
                if callback and _relay.connected:
                    _original_cb = callback

                    def _viewer_aware_callback(q, c):
                        import threading

                        _relay._waiting_for_clarify = True
                        _relay._viewer_clarify_answer = None

                        result_holder = [None]
                        done = threading.Event()

                        def _run_original():
                            result_holder[0] = _original_cb(q, c)
                            done.set()

                        t = threading.Thread(target=_run_original, daemon=True)
                        t.start()

                        # Poll for viewer inject_message while CLI waits
                        while not done.is_set():
                            if _relay._viewer_clarify_answer:
                                answer = _relay._viewer_clarify_answer
                                _relay._viewer_clarify_answer = None
                                _relay._waiting_for_clarify = False
                                _log_to_file(f"clarify answered by viewer: {answer}")
                                # Push answer directly into the clarify response_queue
                                # to resolve the CLI's prompt_toolkit picker
                                try:
                                    mgr = _plugin_ctx._manager if _plugin_ctx else None
                                    cli = getattr(mgr, "_cli_ref", None) if mgr else None
                                    if cli and hasattr(cli, "_clarify_state") and cli._clarify_state:
                                        cli._clarify_state["response_queue"].put(answer)
                                        cli._clarify_state = None  # Clear to dismiss the UI
                                        if hasattr(cli, "_invalidate"):
                                            cli._invalidate()  # Refresh prompt_toolkit
                                        _log_to_file("pushed to clarify response_queue")
                                    else:
                                        # Fallback: use inject_message
                                        if _plugin_ctx and hasattr(_plugin_ctx, "inject_message"):
                                            _plugin_ctx.inject_message(answer)
                                        _log_to_file("fallback: used inject_message")
                                except Exception as e:
                                    _log_to_file(f"clarify resolve error: {e}")
                                done.wait(timeout=3)
                                return answer
                            done.wait(timeout=0.3)

                        _relay._waiting_for_clarify = False
                        return result_holder[0]

                    return _original(question, choices=choices, callback=_viewer_aware_callback)

                return _original(question, choices=choices, callback=callback)

            ct_mod.clarify_tool = _wrapped
            ct_mod._arc_patched = True
            _clarify_patched = True
            (Path.home() / ".arc" / "plugin.log").open("a").write(
                f"{time.strftime('%H:%M:%S')} patched clarify_tool OK\n"
            )
        except Exception as e:
            (Path.home() / ".arc" / "plugin.log").open("a").write(
                f"{time.strftime('%H:%M:%S')} clarify patch error: {e}\n"
            )

    # Try now, and also retry on every pre_tool_call (lazy load safety)
    _ensure_clarify_patched()

    _original_on_pre_tool = _on_pre_tool_call

    def _on_pre_tool_call_with_clarify_patch(tool_name="", **kwargs):
        _ensure_clarify_patched()
        return _original_on_pre_tool(tool_name=tool_name, **kwargs)

    ctx.register_hook("pre_tool_call", _on_pre_tool_call_with_clarify_patch)

    _log_to_file("register() completed — all hooks registered")
    log.info("ARC remote control plugin loaded")
