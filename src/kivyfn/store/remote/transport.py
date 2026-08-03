# kivyfn/store/remote/transport.py
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol


EventBatchHandler = Callable[
    [list[dict[str, Any]]],
    Awaitable[None],
]

TransportHandler = Callable[
    [dict[str, Any]],
    Awaitable[None],
]


class RemoteClientTransport(Protocol):
    """
    Transport contract required by the remote-store client.

    Implementations may use Socket.IO, Unix-domain sockets, TCP, or another
    request/response transport.
    """

    def set_event_handler(
        self,
        handler: EventBatchHandler,
    ) -> None:
        ...

    def set_signal_handler(
        self,
        handler: TransportHandler,
    ) -> None:
        ...

    async def connect(self) -> None:
        ...

    async def disconnect(self) -> None:
        ...

    async def call(
        self,
        event: str,
        message: Any | None = None,
    ) -> Any:
        ...

    @property
    def connected(self) -> bool:
        ...
