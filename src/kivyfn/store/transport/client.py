# src/kivyfn/store/transport/client.py
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from kivyfn.store.protocol.message.requests import Request
from kivyfn.store.protocol.message.responses import Response


ClientEventHandler: TypeAlias = Callable[[str, object | None], Awaitable[None]]

ClientDisconnectHandler: TypeAlias = Callable[[], Awaitable[None]]


class ClientTransport(ABC):
    """
    Transport boundary used by StoreClient.

    A client transport owns only communication mechanics.

    It connects to a Store host, sends requests, receives responses, and
    delivers pushed events through the event handler.
    """

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether the transport is currently connected."""
        raise NotImplementedError

    @abstractmethod
    async def connect(
        self,
        event_handler: ClientEventHandler,
        disconnect_handler: ClientDisconnectHandler,
    ) -> None:
        """Connect and install handlers for events and unexpected loss.

        The disconnect handler is called only after an established connection
        is lost without an explicit disconnect() call. Initial connection
        failures are raised directly from this method.
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect intentionally without calling the disconnect handler."""
        raise NotImplementedError

    @abstractmethod
    async def request(self, request: Request) -> Response:
        """Send one request and return the host response."""
        raise NotImplementedError
