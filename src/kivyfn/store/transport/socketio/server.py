# kivyfn/store/transport/socketio_server.py
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

import socketio
from aiohttp import web

from kivyfn.store.address import Address
from kivyfn.store.operations import OpCode
from kivyfn.store.server.signal import SignalDispatchMessage
from kivyfn.store.server.subscription import DispatchMessage


if TYPE_CHECKING:
    from kivyfn.store.server.proxy import StoreServerProxy


class SocketIOServerTransport:
    """
    Socket.IO server transport for StoreServer.

    This transport owns:

    - the Socket.IO AsyncServer
    - the aiohttp application
    - the aiohttp application runner and TCP site
    - host, port, and Socket.IO path configuration
    - Socket.IO handler registration
    - client connection identifiers
    - Socket.IO room membership
    - wire-payload parsing and validation
    - response serialization
    - subscription dispatch delivery
    - signal dispatch delivery
    - disconnected-client notification

    The transport does not own or access:

    - Engine
    - RemoteSubscriptionManager
    - RemoteSignalManager
    - StoreServer lifecycle or internal state

    Incoming Socket.IO payloads are parsed into concrete Python arguments before
    being passed to the restricted StoreServerProxy.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        socketio_path: str = "socket.io",
        cors_allowed_origins: str | list[str] | None = None,
        logger: bool = False,
        engineio_logger: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._socketio_path = socketio_path

        self._proxy: StoreServerProxy | None = None

        self._server = socketio.AsyncServer(
            async_mode="aiohttp",
            cors_allowed_origins=cors_allowed_origins,
            logger=logger,
            engineio_logger=engineio_logger,
        )

        self._application = web.Application()

        self._server.attach(
            self._application,
            socketio_path=self._socketio_path,
        )

        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._running = False

        self._register_handlers()

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def start(
        self,
        proxy: StoreServerProxy,
    ) -> None:
        """
        Start the Socket.IO server and expose the supplied StoreServerProxy.

        Calling start while already running is safe when the same transport
        instance is used. The proxy is retained only while the transport is
        running.
        """
        if self._running:
            return

        self._proxy = proxy

        runner = web.AppRunner(
            self._application,
        )

        try:
            await runner.setup()

            site = web.TCPSite(
                runner,
                host=self._host,
                port=self._port,
            )

            await site.start()

        except BaseException:
            await runner.cleanup()
            self._proxy = None
            raise

        self._runner = runner
        self._site = site
        self._running = True

    async def stop(self) -> None:
        """
        Stop the Socket.IO server and release all aiohttp resources.

        Calling stop while already stopped is safe.
        """
        if not self._running:
            return

        runner = self._runner

        self._running = False
        self._site = None
        self._runner = None
        self._proxy = None

        if runner is not None:
            await runner.cleanup()

    # -------------------------------------------------
    # Public state
    # -------------------------------------------------

    @property
    def running(self) -> bool:
        """
        Return whether the Socket.IO transport is currently running.
        """
        return self._running

    @property
    def host(self) -> str:
        """
        Return the configured TCP host.
        """
        return self._host

    @property
    def port(self) -> int:
        """
        Return the configured TCP port.
        """
        return self._port

    @property
    def socketio_path(self) -> str:
        """
        Return the configured Socket.IO endpoint path.
        """
        return self._socketio_path

    @property
    def server(self) -> socketio.AsyncServer:
        """
        Return the underlying Socket.IO AsyncServer.

        This is exposed for advanced Socket.IO configuration and inspection.
        Store operations must still go through StoreServerProxy.
        """
        return self._server

    @property
    def application(self) -> web.Application:
        """
        Return the aiohttp application owned by this transport.
        """
        return self._application

    @property
    def endpoint(self) -> dict[str, Any]:
        """
        Return the Socket.IO connection information.
        """
        return {
            "transport": "socketio",
            "host": self._host,
            "port": self._port,
            "socketio_path": self._socketio_path,
        }

    # -------------------------------------------------
    # Proxy access
    # -------------------------------------------------

    def _require_proxy(self) -> StoreServerProxy:
        """
        Return the active StoreServerProxy.

        Raises RuntimeError when a request is handled before the transport has
        been started or after it has been stopped.
        """
        proxy = self._proxy

        if proxy is None:
            raise RuntimeError(
                "Socket.IO server transport is not running"
            )

        return proxy

    # -------------------------------------------------
    # Handler registration
    # -------------------------------------------------

    def _register_handlers(self) -> None:
        """
        Register every Socket.IO request and lifecycle handler.
        """
        self._server.on(
            "connect",
            self._on_connect,
        )

        self._server.on(
            "disconnect",
            self._on_disconnect,
        )

        self._server.on(
            "snapshot",
            self._on_snapshot,
        )

        self._server.on(
            "lookup",
            self._on_lookup,
        )

        self._server.on(
            "to_dict",
            self._on_to_dict,
        )

        self._server.on(
            "subscribe",
            self._on_subscribe,
        )

        self._server.on(
            "unsubscribe",
            self._on_unsubscribe,
        )

        self._server.on(
            "signal_bind",
            self._on_signal_bind,
        )

        self._server.on(
            "signal_unbind",
            self._on_signal_unbind,
        )

        self._server.on(
            "signal_emit",
            self._on_signal_emit,
        )

        self._server.on(
            "transaction",
            self._on_transaction,
        )

        self._server.on(
            "undo",
            self._on_undo,
        )

        self._server.on(
            "redo",
            self._on_redo,
        )

    # -------------------------------------------------
    # Connection lifecycle
    # -------------------------------------------------

    async def _on_connect(
        self,
        sid: str,
        environ: dict[str, Any],
        auth: Any = None,
    ) -> bool:
        """
        Accept a new Socket.IO client connection.
        """
        return True

    async def _on_disconnect(
        self,
        sid: str,
        reason: str | None = None,
    ) -> None:
        """
        Notify StoreServer that a Socket.IO client disconnected.
        """
        proxy = self._proxy

        if proxy is None:
            return

        proxy.disconnect_client(
            client_id=sid,
        )

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

    async def _on_snapshot(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and execute a snapshot request.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().snapshot(
                address=self._parse_address(
                    payload,
                ),
                id=self._parse_optional_string(
                    payload,
                    "id",
                ),
                key=self._parse_optional_string(
                    payload,
                    "key",
                ),
                resolve_foreignkeys=self._parse_bool(
                    payload,
                    "resolve_foreignkeys",
                    default=False,
                ),
            )

            return self._success(
                result,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    async def _on_lookup(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and execute a lookup request.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().lookup(
                address=self._parse_address(
                    payload,
                ),
                fields=self._parse_lookup_fields(
                    payload,
                ),
                value=payload.get(
                    "value",
                ),
                first=self._parse_bool(
                    payload,
                    "first",
                    default=False,
                ),
            )

            return self._success(
                result,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    async def _on_to_dict(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and execute a complete-node snapshot request.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().to_dict(
                address=self._parse_address(
                    payload,
                ),
            )

            return self._success(
                result,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    async def _on_subscribe(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and register a store-event subscription.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().subscribe(
                client_id=sid,
                opcode=self._parse_opcode(
                    payload,
                ),
                address=self._parse_address(
                    payload,
                ),
                id=self._parse_optional_string(
                    payload,
                    "id",
                ),
                key=self._parse_optional_string(
                    payload,
                    "key",
                ),
                depth=self._parse_depth(
                    payload,
                ),
                resolve_foreignkeys=self._parse_bool(
                    payload,
                    "resolve_foreignkeys",
                    default=False,
                ),
                propagate_foreignkeys=self._parse_bool(
                    payload,
                    "propagate_foreignkeys",
                    default=False,
                ),
            )

            if result.should_join_channel:
                await self._server.enter_room(
                    sid,
                    result.channel,
                )

            return self._success(
                {
                    "subscription_token": result.subscription_token,
                    "event_token": result.event_token,
                }
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    async def _on_unsubscribe(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and remove a store-event subscription.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().unsubscribe(
                client_id=sid,
                subscription_token=self._require_string(
                    payload,
                    "subscription_token",
                ),
            )

            if result.should_leave_channel:
                await self._server.leave_room(
                    sid,
                    result.channel,
                )

            return self._success(
                True,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    async def _on_signal_bind(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and register a remote signal binding.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().bind_signal(
                client_id=sid,
                address=self._parse_address(
                    payload,
                ),
                signal=self._require_string(
                    payload,
                    "signal",
                ),
                id=self._parse_optional_string(
                    payload,
                    "id",
                ),
            )

            if result.should_join_channel:
                await self._server.enter_room(
                    sid,
                    result.channel,
                )

            return self._success(
                {
                    "binding_token": result.binding_token,
                    "event_token": result.event_token,
                }
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    async def _on_signal_unbind(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and remove a remote signal binding.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().unbind_signal(
                client_id=sid,
                binding_token=self._require_string(
                    payload,
                    "binding_token",
                ),
            )

            if result.should_leave_channel:
                await self._server.leave_room(
                    sid,
                    result.channel,
                )

            return self._success(
                True,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    async def _on_signal_emit(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse, emit, and dispatch a store signal.
        """
        try:
            payload = self._require_message(
                message,
            )

            messages = self._require_proxy().emit_signal(
                address=self._parse_address(
                    payload,
                ),
                signal=self._require_string(
                    payload,
                    "signal",
                ),
                data=payload.get(
                    "data",
                ),
                id=self._parse_optional_string(
                    payload,
                    "id",
                ),
            )

            await self._emit_signal_messages(
                messages,
            )

            return self._success(
                True,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    async def _on_transaction(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        """
        Parse and execute a store transaction.
        """
        try:
            payload = self._require_message(
                message,
            )

            result = self._require_proxy().transaction(
                instructions=self._parse_instructions(
                    payload,
                ),
            )

            await self._emit_subscription_messages(
                result.dispatches,
            )

            return self._success(
                result.result,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    async def _on_undo(
        self,
        sid: str,
        message: Any = None,
    ) -> dict[str, Any]:
        """
        Undo the latest reversible transaction and dispatch its events.
        """
        try:
            result = self._require_proxy().undo()

            await self._emit_subscription_messages(
                result.dispatches,
            )

            return self._success(
                result.result,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    async def _on_redo(
        self,
        sid: str,
        message: Any = None,
    ) -> dict[str, Any]:
        """
        Redo the latest undone transaction and dispatch its events.
        """
        try:
            result = self._require_proxy().redo()

            await self._emit_subscription_messages(
                result.dispatches,
            )

            return self._success(
                result.result,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
            )

    # -------------------------------------------------
    # Event dispatch
    # -------------------------------------------------

    async def _emit_subscription_messages(
        self,
        messages: tuple[DispatchMessage, ...],
    ) -> None:
        """
        Deliver subscription messages through Socket.IO rooms.
        """
        for message in messages:
            await self._server.emit(
                "event",
                message.payload,
                room=message.channel,
            )

    async def _emit_signal_messages(
        self,
        messages: tuple[SignalDispatchMessage, ...],
    ) -> None:
        """
        Deliver signal messages through Socket.IO rooms.
        """
        for message in messages:
            await self._server.emit(
                "signal",
                message.payload,
                room=message.channel,
            )

    # -------------------------------------------------
    # Payload parsing
    # -------------------------------------------------

    @staticmethod
    def _require_message(
        message: Any,
    ) -> dict[str, Any]:
        """
        Require a Socket.IO request payload object.
        """
        if not isinstance(
            message,
            dict,
        ):
            raise TypeError(
                "Socket.IO request payload must be an object"
            )

        return message

    @staticmethod
    def _parse_address(
        message: dict[str, Any],
    ) -> Address[Any]:
        """
        Parse a store Address from a Socket.IO payload.
        """
        value = message.get(
            "address",
        )

        if not isinstance(
            value,
            list,
        ):
            raise TypeError(
                "Store address must be a list"
            )

        return Address(
            *value,
        )

    @staticmethod
    def _parse_opcode(
        message: dict[str, Any],
    ) -> OpCode:
        """
        Parse a concrete subscription OpCode.
        """
        value = message.get(
            "opcode",
        )

        try:
            return OpCode(
                value,
            )
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "Subscription opcode is invalid"
            ) from exc

    @staticmethod
    def _parse_lookup_fields(
        message: dict[str, Any],
    ) -> str | tuple[str, ...]:
        """
        Parse lookup fields from a string or list of strings.
        """
        value = message.get(
            "fields",
        )

        if isinstance(
            value,
            str,
        ):
            if not value:
                raise TypeError(
                    "Lookup field must be non-empty"
                )

            return value

        if isinstance(
            value,
            list,
        ):
            if not value or not all(
                isinstance(field, str) and field
                for field in value
            ):
                raise TypeError(
                    "Lookup fields must contain non-empty strings"
                )

            return tuple(
                value,
            )

        raise TypeError(
            "Lookup fields must be a string or list of strings"
        )

    @staticmethod
    def _require_string(
        message: dict[str, Any],
        key: str,
    ) -> str:
        """
        Parse a required non-empty string.
        """
        value = message.get(
            key,
        )

        if not isinstance(
            value,
            str,
        ) or not value:
            raise TypeError(
                f"{key} must be a non-empty string"
            )

        return value

    @staticmethod
    def _parse_optional_string(
        message: dict[str, Any],
        key: str,
    ) -> str | None:
        """
        Parse an optional string.
        """
        value = message.get(
            key,
        )

        if (
            value is not None
            and not isinstance(value, str)
        ):
            raise TypeError(
                f"{key} must be a string or None"
            )

        return value

    @staticmethod
    def _parse_bool(
        message: dict[str, Any],
        key: str,
        *,
        default: bool,
    ) -> bool:
        """
        Parse a concrete boolean option.
        """
        value = message.get(
            key,
            default,
        )

        if not isinstance(
            value,
            bool,
        ):
            raise TypeError(
                f"{key} must be a boolean"
            )

        return value

    @staticmethod
    def _parse_depth(
        message: dict[str, Any],
    ) -> int | None:
        """
        Parse foreign-key subscription depth.

        None represents unlimited propagation. Integer values must be zero or
        positive.
        """
        value = message.get(
            "depth",
            0,
        )

        if value is None:
            return None

        if (
            isinstance(value, bool)
            or not isinstance(value, int)
        ):
            raise TypeError(
                "Subscription depth must be an integer or None"
            )

        if value < 0:
            raise ValueError(
                "Subscription depth must be zero, positive, or None"
            )

        return value

    @staticmethod
    def _parse_instructions(
        message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Parse a transaction instruction list.
        """
        value = message.get(
            "instructions",
        )

        if not isinstance(
            value,
            list,
        ):
            raise TypeError(
                "Transaction instructions must be a list"
            )

        if not all(
            isinstance(instruction, dict)
            for instruction in value
        ):
            raise TypeError(
                "Every transaction instruction must be an object"
            )

        return value

    # -------------------------------------------------
    # Response serialization
    # -------------------------------------------------

    @classmethod
    def _success(
        cls,
        data: Any,
        *,
        status: int = 200,
    ) -> dict[str, Any]:
        """
        Build a successful Socket.IO acknowledgement envelope.
        """
        return {
            "ok": True,
            "status": status,
            "data": cls._serialize(
                data,
            ),
            "error": None,
        }

    @staticmethod
    def _failure(
        error: str,
        *,
        status: int,
    ) -> dict[str, Any]:
        """
        Build a failed Socket.IO acknowledgement envelope.
        """
        return {
            "ok": False,
            "status": status,
            "data": None,
            "error": error,
        }

    @classmethod
    def _failure_from_exception(
        cls,
        exc: Exception,
    ) -> dict[str, Any]:
        """
        Convert an exception into a Socket.IO acknowledgement failure.
        """
        if isinstance(
            exc,
            KeyError,
        ):
            status = 404

        elif isinstance(
            exc,
            (TypeError, ValueError),
        ):
            status = 400

        else:
            status = 500

        message = str(
            exc,
        )

        if not message:
            message = type(
                exc,
            ).__name__

        return cls._failure(
            message,
            status=status,
        )

    @classmethod
    def _serialize(
        cls,
        value: Any,
    ) -> Any:
        """
        Convert store-domain values into Socket.IO-compatible values.
        """
        if value is None:
            return None

        if isinstance(
            value,
            (str, int, float, bool),
        ):
            return value

        if isinstance(
            value,
            Address,
        ):
            return value.to_list()

        if isinstance(
            value,
            dict,
        ):
            return {
                str(key): cls._serialize(
                    item,
                )
                for key, item in value.items()
            }

        if isinstance(
            value,
            (list, tuple, set),
        ):
            return [
                cls._serialize(
                    item,
                )
                for item in value
            ]

        if (
            is_dataclass(value)
            and not isinstance(value, type)
        ):
            return cls._serialize(
                asdict(
                    value,
                )
            )

        payload = getattr(
            value,
            "payload",
            None,
        )

        if callable(
            payload,
        ):
            return cls._serialize(
                payload(),
            )

        to_dict = getattr(
            value,
            "to_dict",
            None,
        )

        if callable(
            to_dict,
        ):
            return cls._serialize(
                to_dict(),
            )

        raise TypeError(
            "Value is not Socket.IO serializable: "
            f"{type(value).__name__}"
        )
