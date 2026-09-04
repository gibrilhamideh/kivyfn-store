from __future__ import annotations

from enum import Enum


class ConnectionState(str, Enum):
    """Lifecycle state of one remote-store connection."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    SYNCHRONIZING = "synchronizing"
    READY = "ready"
    DISCONNECTING = "disconnecting"
