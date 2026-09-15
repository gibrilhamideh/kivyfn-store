# src/kivyfn/store/devtools/config.py
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address


@dataclass(frozen=True, slots=True)
class DevToolsConfig:
    """Local listener and independently bounded diagnostic capture."""

    host: str = "127.0.0.1"
    port: int = 18766
    max_records: int = 1000
    max_bytes: int = 8 * 1024 * 1024
    max_payload_bytes: int = 1024 * 1024
    max_queue_bytes: int = 2 * 1024 * 1024
    max_queue_messages: int = 256
    max_request_bytes: int = 1024 * 1024
    max_pending_captures: int = 256
    max_pending_commands: int = 64
    max_sessions: int = 16
    max_filter_values: int = 64

    def __post_init__(self) -> None:
        if not ip_address(self.host).is_loopback:
            raise ValueError("Devtools must bind a loopback IP address")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("Devtools port must be in 0..65535")
        for name in self.__dataclass_fields__:
            if name in {"host", "port"}:
                continue
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"Devtools {name} must be a positive integer")
