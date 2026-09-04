# kivyfn/store/transport/socketio/client.py
from __future__ import annotations

from typing import Any

import socketio
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from kivyfn.logging import get_logger
from kivyfn.store.remote.transport import (
    EventBatchHandler,
    LifecycleHandler,
    TransportHandler,
)
from kivyfn.store.error import (
    StoreConnectionError,
    StoreProtocolError,
    StoreStateError,
    StoreTimeoutError,
)

logger = get_logger(__name__)


class SocketIOClientTransport:
    """
    Asynchronous Socket.IO transport for RemoteStoreClient.

    Responsibilities:

    - own and configure python-socketio's AsyncClient;
    - connect and disconnect from the remote endpoint;
    - perform request/response calls;
    - translate Socket.IO failures into store runtime errors;
    - forward incoming store event batches and signals to handlers.

    This transport contains no store, schema, transaction, subscription,
    signal, or response-domain logic.
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

        self._event_handler: EventBatchHandler | None = None
        self._signal_handler: TransportHandler | None = None
        self._lifecycle_handler: LifecycleHandler | None = None

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
            "connect",
            self._on_connect,
        )

        self._client.on(
            "disconnect",
            self._on_disconnect,
        )

        self._client.on(
            "events",
            self._on_events,
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
        handler: EventBatchHandler,
    ) -> None:
        self._event_handler = handler

    def set_signal_handler(
        self,
        handler: TransportHandler,
    ) -> None:
        self._signal_handler = handler

    def set_lifecycle_handler(
        self,
        handler: LifecycleHandler,
    ) -> None:
        self._lifecycle_handler = handler

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def connect(self) -> None:
        """
        Connect to the configured Socket.IO endpoint.

        Calling connect while already connected is safe.
        """
        if self.connected:
            return

        self._require_handlers()

        try:
            await self._client.connect(
                self.url,
                wait=True,
                wait_timeout=self.timeout,
            )

        except SocketIOTimeoutError as exc:
            raise StoreTimeoutError(
                "Socket.IO connection timed out for "
                f"{self.url!r}"
            ) from exc

        except Exception as exc:
            raise StoreConnectionError(
                "Failed to connect to remote store at "
                f"{self.url!r}: {type(exc).__name__}: {exc}"
            ) from exc

        if not self.connected:
            raise StoreConnectionError(
                f"Socket.IO connection to {self.url!r} "
                "did not become active"
            )

    async def disconnect(self) -> None:
        """
        Disconnect from the Socket.IO endpoint.

        Calling disconnect while already disconnected is safe.
        """
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
        return bool(self._client.connected)

    # -------------------------------------------------
    # Calls
    # -------------------------------------------------

    async def call(
        self,
        event: str,
        message: Any | None = None,
    ) -> Any:
        """
        Perform one Socket.IO acknowledgement call.

        This method only returns the raw acknowledgement payload.
        Interpreting the store response belongs to RemoteStoreClient.
        """
        if not self.connected:
            raise StoreStateError(
                "Socket.IO transport is not connected"
            )

        try:
            return await self._client.call(
                event,
                message,
                timeout=self.timeout,
            )

        except SocketIOTimeoutError as exc:
            raise StoreTimeoutError(
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

    async def _on_connect(self) -> None:
        handler = self._lifecycle_handler

        if handler is not None:
            handler("connected")

    async def _on_disconnect(
        self,
        _reason: str | None = None,
    ) -> None:
        handler = self._lifecycle_handler

        if handler is not None:
            handler("disconnected")

    async def _on_events(
        self,
        message: Any,
    ) -> None:
        """
        Validate and forward one incoming transaction event batch.
        """
        handler = self._event_handler

        if handler is None:
            raise StoreStateError(
                "Socket.IO event handler has not been configured"
            )

        if not isinstance(message, dict):
            raise StoreProtocolError(
                "Socket.IO store events payload must be an object"
            )

        events = message.get("events")

        if not isinstance(events, list):
            raise StoreProtocolError(
                "Socket.IO store events payload must contain "
                "an events list"
            )

        if not all(isinstance(event, dict) for event in events):
            raise StoreProtocolError(
                "Every Socket.IO store event must be an object"
            )

        try:
            await handler(events)

        except StoreProtocolError:
            raise

        except Exception:
            logger.exception(
                "Remote store event batch handler failed"
            )

    async def _on_signal(
        self,
        message: Any,
    ) -> None:
        """
        Validate and forward one incoming signal dispatch.
        """
        handler = self._signal_handler

        if handler is None:
            raise StoreStateError(
                "Socket.IO signal handler has not been configured"
            )

        if not isinstance(message, dict):
            raise StoreProtocolError(
                "Socket.IO signal payload must be an object"
            )

        try:
            await handler(message)

        except StoreProtocolError:
            raise

        except Exception:
            logger.exception(
                "Remote store signal handler failed"
            )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    def _require_handlers(self) -> None:
        if self._event_handler is None:
            raise StoreStateError(
                "Socket.IO event handler must be configured "
                "before connecting"
            )

        if self._signal_handler is None:
            raise StoreStateError(
                "Socket.IO signal handler must be configured "
                "before connecting"
            )

        if self._lifecycle_handler is None:
            raise StoreStateError(
                "Socket.IO lifecycle handler must be configured "
                "before connecting"
            )


