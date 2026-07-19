# kivyfn/store/transport/socketio/client.py
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import socketio
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from kivyfn.store.errors.client import (
    StoreConnectionError,
    StoreStateError,
)


TransportHandler = Callable[
    [dict[str, Any]],
    Awaitable[None],
]


class SocketIOClientTransport:
    """
    Asynchronous Socket.IO transport for RemoteStoreClient.

    Responsibilities:
    - own and configure python-socketio's AsyncClient
    - connect and disconnect from the remote endpoint
    - perform request/response calls
    - forward incoming store events and signals to registered handlers

    This class contains no store, schema, transaction, subscription, or signal
    domain logic.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: int = 5,
        reconnection: bool = True,
        reconnection_attempts: int = 0,
        reconnection_delay: int = 1,
        reconnection_delay_max: int = 5,
        randomization_factor: float = 0.5,
        logger: bool = False,
        engineio_logger: bool = False,
    ) -> None:
        if timeout <= 0:
            raise ValueError(
                "Socket.IO timeout must be greater than zero"
            )

        if reconnection_attempts < 0:
            raise ValueError(
                "Socket.IO reconnection attempts cannot be negative"
            )

        if reconnection_delay < 0:
            raise ValueError(
                "Socket.IO reconnection delay cannot be negative"
            )

        if reconnection_delay_max < reconnection_delay:
            raise ValueError(
                "Socket.IO maximum reconnection delay cannot be smaller "
                "than the initial reconnection delay"
            )

        if randomization_factor < 0:
            raise ValueError(
                "Socket.IO randomization factor cannot be negative"
            )

        self.url = url
        self.timeout = timeout

        self._event_handler: TransportHandler | None = None
        self._signal_handler: TransportHandler | None = None

        self._client = socketio.AsyncClient(
            reconnection=reconnection,
            reconnection_attempts=reconnection_attempts,
            reconnection_delay=reconnection_delay,
            reconnection_delay_max=reconnection_delay_max,
            randomization_factor=randomization_factor,
            logger=logger,
            engineio_logger=engineio_logger,
        )

        self._client.on(
            "event",
            self._on_event,
        )

        self._client.on(
            "signal",
            self._on_signal,
        )

    # -------------------------------------------------
    # Handler registration
    # -------------------------------------------------

    def set_event_handler(
        self,
        handler: TransportHandler,
    ) -> None:
        self._event_handler = handler

    def set_signal_handler(
        self,
        handler: TransportHandler,
    ) -> None:
        self._signal_handler = handler

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def connect(self) -> None:
        if self.connected:
            return

        self._require_handlers()

        try:
            await self._client.connect(
                self.url,
                wait=True,
                wait_timeout=self.timeout,
            )

        except Exception as exc:
            raise StoreConnectionError(
                "Failed to connect to remote store at "
                f"{self.url!r}: {type(exc).__name__}: {exc}"
            ) from exc

        if not self.connected:
            raise StoreConnectionError(
                f"Socket.IO connection to {self.url!r} did not become active"
            )

    async def disconnect(self) -> None:
        if not self.connected:
            return

        try:
            await self._client.disconnect()

        except Exception as exc:
            raise StoreConnectionError(
                "Failed to disconnect from remote store at "
                f"{self.url!r}: {type(exc).__name__}: {exc}"
            ) from exc

    @property
    def connected(self) -> bool:
        return bool(
            self._client.connected
        )

    # -------------------------------------------------
    # Calls
    # -------------------------------------------------

    async def call(
        self,
        event: str,
        message: Any | None = None,
    ) -> Any:
        if not self.connected:
            raise StoreConnectionError(
                "Socket.IO transport is not connected"
            )

        try:
            return await self._client.call(
                event,
                message,
                timeout=self.timeout,
            )

        except SocketIOTimeoutError as exc:
            raise StoreConnectionError(
                f"Socket.IO call timed out: {event!r}"
            ) from exc

        except Exception as exc:
            raise StoreConnectionError(
                f"Socket.IO call failed: {event!r}; "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    # -------------------------------------------------
    # Incoming events
    # -------------------------------------------------

    async def _on_event(
        self,
        message: dict[str, Any],
    ) -> None:
        handler = self._event_handler

        if handler is None:
            raise StoreStateError(
                "Socket.IO event handler has not been configured"
            )

        if not isinstance(
            message,
            dict,
        ):
            raise ValueError(
                "Socket.IO store event payload must be an object"
            )

        await handler(
            message,
        )

    async def _on_signal(
        self,
        message: dict[str, Any],
    ) -> None:
        handler = self._signal_handler

        if handler is None:
            raise StoreStateError(
                "Socket.IO signal handler has not been configured"
            )

        if not isinstance(
            message,
            dict,
        ):
            raise ValueError(
                "Socket.IO signal payload must be an object"
            )

        await handler(
            message,
        )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    def _require_handlers(self) -> None:
        if self._event_handler is None:
            raise StoreStateError(
                "Socket.IO event handler must be configured before connecting"
            )

        if self._signal_handler is None:
            raise StoreStateError(
                "Socket.IO signal handler must be configured before connecting"
            )


