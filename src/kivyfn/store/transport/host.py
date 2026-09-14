# src/kivyfn/store/transport/host.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from kivyfn.store.protocol.message.event import EventMessage

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost


class HostTransport(ABC):
    """Transport used by StoreHost to communicate with clients."""

    store_host: StoreHost

    @property
    @abstractmethod
    def running(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def endpoint(self) -> dict[str, Any]:
        """Describe the bound transport endpoint."""
        raise NotImplementedError

    @abstractmethod
    async def start(self, store_host: StoreHost) -> None:
        """Start the transport with its StoreHost."""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """Stop the transport."""
        raise NotImplementedError

    @abstractmethod
    async def emit(self, client_id: str, message: EventMessage) -> None:
        """Deliver one EventMessage to one client."""
        raise NotImplementedError
