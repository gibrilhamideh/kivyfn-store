from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol


EventBatchHandler = Callable[
    [list[dict[str, Any]]],
    Awaitable[None],
]

TransportHandler = Callable[
    [dict[str, Any]],
    Awaitable[None],
]

LifecycleEvent = Literal[
    "connected",
    "disconnected",
]

LifecycleHandler = Callable[
    [LifecycleEvent],
    None,
]


class RemoteClientTransport(Protocol):
    """Transport contract required by the remote-store client."""

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

    def set_lifecycle_handler(
        self,
        handler: LifecycleHandler,
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
