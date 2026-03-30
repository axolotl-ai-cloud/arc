"""
Data models for the relay server.

Extracted so protocols and defaults can reference them without importing relay.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import WebSocket


@dataclass
class SessionInfo:
    session_id: str
    agent_framework: str
    agent_name: str | None = None
    started_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    e2e: str | None = None  # "session_secret", "passkey", "passphrase", or None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "sessionId": self.session_id,
            "agentFramework": self.agent_framework,
            "agentName": self.agent_name,
            "startedAt": self.started_at,
        }
        if self.e2e:
            d["e2e"] = self.e2e
        return d


@dataclass
class Session:
    agent_ws: WebSocket
    info: SessionInfo
    session_secret: str
    user_id: str | None = None
    tenant_id: str | None = None  # Org/team that owns this session (for multi-tenant)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    viewers: set[WebSocket] = field(default_factory=set)
    traces: list[dict] = field(default_factory=list)
