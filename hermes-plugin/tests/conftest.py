"""Make the ArcRelay class importable in tests."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

# Mock hermes_cli and websocket so the plugin module can be imported
# without Hermes or websocket-client installed
sys.modules.setdefault("hermes_cli", MagicMock())
sys.modules.setdefault("hermes_cli.plugins", MagicMock())
ws_mock = MagicMock()
ws_mock.WebSocketTimeoutException = type("WebSocketTimeoutException", (Exception,), {})
sys.modules["websocket"] = ws_mock

# Try to import cryptography; if it fails, mock it with a simple XOR-based "encryption"
# (not real crypto, just for testing the pipeline)
# Mock cryptography with a simple XOR-based fake for testing
# (the real cryptography package may not be available in all envs)
for _mod in list(sys.modules):
    if _mod.startswith("cryptography"):
        del sys.modules[_mod]

if True:  # Always use mock for consistent testing

    class _FakeAESGCM:
        def __init__(self, key):
            self._key = key

        def encrypt(self, nonce, data, aad):
            # Simple XOR for testing (NOT real encryption)
            key_bytes = self._key * (len(data) // len(self._key) + 1)
            return bytes(a ^ b for a, b in zip(data, key_bytes[: len(data)], strict=False)) + nonce

        def decrypt(self, nonce, data, aad):
            ct = data[: -len(nonce)]
            key_bytes = self._key * (len(ct) // len(self._key) + 1)
            return bytes(a ^ b for a, b in zip(ct, key_bytes[: len(ct)], strict=False))

    _aead_mod = MagicMock()
    _aead_mod.AESGCM = _FakeAESGCM

    _ciphers_mod = MagicMock()
    _ciphers_mod.aead = _aead_mod
    _ciphers_mod.AESGCM = _FakeAESGCM

    _prims_mod = MagicMock()
    _prims_mod.ciphers = _ciphers_mod

    _hashes_mod = MagicMock()
    _hashes_mod.SHA256 = MagicMock

    # Don't mock HKDF — let the plugin use its manual fallback
    # Only mock the AESGCM class for encrypt/decrypt

    _hazmat_mod = MagicMock()
    _hazmat_mod.primitives = _prims_mod
    _hazmat_mod.primitives.ciphers = _ciphers_mod
    _hazmat_mod.primitives.ciphers.aead = _aead_mod

    crypto_mod = MagicMock()
    crypto_mod.hazmat = _hazmat_mod

    sys.modules["cryptography"] = crypto_mod
    sys.modules["cryptography.hazmat"] = _hazmat_mod
    sys.modules["cryptography.hazmat.primitives"] = _prims_mod
    sys.modules["cryptography.hazmat.primitives.ciphers"] = _ciphers_mod
    sys.modules["cryptography.hazmat.primitives.ciphers.aead"] = _aead_mod
    # Don't mock hashes/kdf — let _derive_e2e_key fall through to manual HKDF

# Import the plugin's __init__.py via importlib (can't use normal import due to hyphen)
plugin_init = Path(__file__).parent.parent / "arc-remote-control" / "__init__.py"

with patch("subprocess.check_call"):
    spec = importlib.util.spec_from_file_location("arc_remote_control", plugin_init)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

# Expose as hermes_plugin_path for test imports
proxy = ModuleType("hermes_plugin_path")
proxy.ArcRelay = mod.ArcRelay  # type: ignore
sys.modules["hermes_plugin_path"] = proxy
