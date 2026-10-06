from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, TypeAlias
from urllib.parse import urlsplit

RelayCredentials: TypeAlias = Mapping[str, Any]
RelayAuthProvider: TypeAlias = Callable[
    [], RelayCredentials | Awaitable[RelayCredentials]
]


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """An optional outward Socket.IO connection for an existing StoreHost.

    Authentication is evaluated on every connection attempt. A provider
    must not block the host event loop. For StoreProcess, use a module-level
    callable or a picklable callable object, and obtain secrets in the child.
    Endpoint paths select the Socket.IO path, as with StoreClient endpoints.
    """

    endpoint: str
    store_id: str
    auth: RelayCredentials | RelayAuthProvider | None = field(
        default=None, repr=False
    )
    reconnect_interval: float = 10.0
    connect_timeout: float = 10.0
    request_timeout: float = 10.0
    max_clients: int = 64
    max_pending_messages: int = 64
    max_pending_bytes: int = 4 * 1024 * 1024
    max_frame_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str):
            raise TypeError("Relay endpoint must be a string")
        endpoint = urlsplit(self.endpoint)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError("Relay endpoint must be an HTTP(S) URL")
        if endpoint.username is not None or endpoint.password is not None:
            raise ValueError("Relay credentials belong in auth, not the URL")
        if endpoint.query or endpoint.fragment:
            raise ValueError("Relay endpoint cannot have a query or fragment")
        if endpoint.port == 0:
            raise ValueError("Relay endpoint requires a nonzero port")
        if not isinstance(self.store_id, str) or not self.store_id.strip():
            raise ValueError("Relay store_id must be a non-empty string")
        if self.auth is not None and not (
            isinstance(self.auth, Mapping) or callable(self.auth)
        ):
            raise TypeError("Relay auth must be a mapping or callable")
        for name in (
            "reconnect_interval",
            "connect_timeout",
            "request_timeout",
        ):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"Relay {name} must be finite and positive")
        for name in (
            "max_clients",
            "max_pending_messages",
            "max_pending_bytes",
            "max_frame_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"Relay {name} must be a positive integer")
