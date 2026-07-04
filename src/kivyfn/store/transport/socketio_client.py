from __future__ import annotations
from typing import Any, Awaitable, Callable

import socketio
from kivyfn.store.errors.client import StoreConnectionError
EventCallback = Callable[[dict[str, Any]], None | Awaitable[None]]


class SocketIOClient:
    def __init__(
        self,
        url: str,
        *,
        event_hanlder: EventCallback,
        timeout: int = 5,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.event_hanlder = event_hanlder

        self.sio = socketio.AsyncClient(
            reconnection=True,
            logger=False,
            engineio_logger=False,
        )

        self.sio.on("event", self.on_event)

    async def connect(self) -> None:
        await self.sio.connect(self.url)

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

    async def on_event(self, message: dict[str, Any]) -> None:
        self.event_hanlder(message)
