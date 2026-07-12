# kivyfn/store/transport/socketio_client.py
from __future__ import annotations

from typing import Any, Awaitable, Callable

import socketio

from kivyfn.store.errors.client import StoreConnectionError


TransportCallback = Callable[
    [dict[str, Any]],
    Awaitable[None],
]


class SocketIOClient:
    def __init__(
        self,
        url: str,
        *,
        event_handler: TransportCallback,
        signal_handler: TransportCallback,
        timeout: int = 5,
    ) -> None:
        self.url = url
        self.timeout = timeout

        self.event_handler = event_handler
        self.signal_handler = signal_handler

        self.sio = socketio.AsyncClient(
            reconnection=True,
            logger=False,
            engineio_logger=False,
        )

        self.sio.on(
            "event",
            self.on_event,
        )
        self.sio.on(
            "signal",
            self.on_signal,
        )

    async def connect(self) -> None:
        await self.sio.connect(
            self.url,
        )

    async def disconnect(self) -> None:
        await self.sio.disconnect()

    async def call(
        self,
        event: str,
        message: Any | None = None,
    ) -> Any:
        if not self.sio.connected:
            raise StoreConnectionError(
                "SocketIOClient is not connected"
            )

        try:
            return await self.sio.call(
                event,
                message,
                timeout=self.timeout,
            )

        except Exception as exc:
            raise StoreConnectionError(
                f"SocketIO call failed: {event!r}; "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def on_event(
        self,
        message: dict[str, Any],
    ) -> None:
        await self.event_handler(
            message,
        )

    async def on_signal(
        self,
        message: dict[str, Any],
    ) -> None:
        await self.signal_handler(
            message,
        )


