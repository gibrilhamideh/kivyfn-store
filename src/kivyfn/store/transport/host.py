# src/kivyfn/store/transport/host.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from kivyfn.store.protocol.message.event import EventMessage

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost


class HostTransport(ABC):
    """Connect clients to a StoreHost and carry its typed messages."""

    store_host: StoreHost

    @abstractmethod
    async def start(self, store_host: StoreHost) -> None:
        """Bind the host before accepting clients."""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """Stop accepting clients and release transport resources."""
        raise NotImplementedError

    @abstractmethod
    async def emit(self, client_id: str, message: EventMessage) -> None:
        """Deliver one typed event message to one client."""
        raise NotImplementedError

    @property
    @abstractmethod
    def running(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def endpoint(self) -> dict[str, Any]:
        """Describe the bound transport address for process readiness."""
        raise NotImplementedError
