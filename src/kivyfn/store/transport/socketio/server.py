# kivyfn/store/transport/socketio/server.py
from __future__ import annotations

import asyncio

from collections import defaultdict
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any, cast

import socketio

from aiohttp import web

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.error import (
    InstructionErrorDetail,
    InvalidInstructionError,
    StoreErrorCategory,
    StoreErrorCode,
    StoreErrorStatus,
    StoreFailure,
    StoreOperationError,
)
from kivyfn.store.operations import OpCode
from kivyfn.store.query import query_from_payload
from kivyfn.store.response import (
    failure,
    success,
)
from kivyfn.store.server.signal import SignalDispatchMessage
from kivyfn.store.server.subscription import DispatchMessage


if TYPE_CHECKING:
    from kivyfn.store.server.proxy import StoreServerProxy
    from kivyfn.store.core.execution import ExecutionResult


logger = get_logger(__name__)


class SocketIOServerTransport:
    """
    Socket.IO server transport for StoreServer.

    This transport owns:

    - the Socket.IO AsyncServer;
    - the aiohttp application;
    - the aiohttp application runner and TCP site;
    - host, port, and Socket.IO path configuration;
    - Socket.IO handler registration;
    - client connection identifiers;
    - Socket.IO room membership;
    - wire-payload parsing and validation;
    - response serialization;
    - subscription dispatch delivery;
    - signal dispatch delivery;
    - disconnected-client notification.

    The transport does not own or access:

    - Engine;
    - RemoteSubscriptionManager;
    - RemoteSignalManager;
    - StoreServer lifecycle or internal state.

    Incoming Socket.IO payloads are parsed into concrete Python arguments
    before being passed to the restricted StoreServerProxy.
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
        self._mutation_lock = asyncio.Lock()
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
        Start the Socket.IO server and expose the supplied proxy.

        Calling start while already running is safe.
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
        Stop the Socket.IO server and release aiohttp resources.

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
        return self._running

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def socketio_path(self) -> str:
        return self._socketio_path

    @property
    def server(self) -> socketio.AsyncServer:
        return self._server

    @property
    def application(self) -> web.Application:
        return self._application

    @property
    def endpoint(self) -> dict[str, Any]:
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
            "sync_snapshot",
            self._on_sync_snapshot,
        )

        self._server.on(
            "revision_audit",
            self._on_revision_audit,
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
            "query_fetch",
            self._on_query_fetch,
        )

        self._server.on(
            "query_subscribe",
            self._on_query_subscribe,
        )

        self._server.on(
            "query_unsubscribe",
            self._on_query_unsubscribe,
        )

        self._server.on(
            "query_sync",
            self._on_query_sync,
        )

        self._server.on(
            "query_audit",
            self._on_query_audit,
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
        return True

    async def _on_disconnect(
        self,
        sid: str,
        reason: str | None = None,
    ) -> None:
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
                operation="snapshot",
            )

    async def _on_sync_snapshot(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            payload = self._require_message(
                message,
            )
            result = self._require_proxy().sync_snapshot(
                address=self._parse_address(
                    payload,
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
                operation="sync_snapshot",
            )

    async def _on_revision_audit(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            payload = self._require_message(
                message,
            )
            result = self._require_proxy().revision_audit(
                self._parse_addresses(
                    payload,
                )
            )

            return self._success(
                result,
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="revision_audit",
            )

    async def _on_lookup(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
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
                operation="lookup",
            )

    async def _on_to_dict(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
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
                operation="to_dict",
            )

    # -------------------------------------------------
    # Queries
    # -------------------------------------------------

    async def _on_query_fetch(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            payload = self._require_message(message)
            query = query_from_payload(payload.get("query"))
            result = self._require_proxy().fetch(query)
            return self._success(result)
        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="query_fetch",
            )

    async def _on_query_subscribe(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            async with self._mutation_lock:
                payload = self._require_message(message)
                query = query_from_payload(payload.get("query"))
                result = self._require_proxy().subscribe_query(
                    sid,
                    query,
                )

                if result.should_join_channel:
                    await self._server.enter_room(
                        sid,
                        result.channel,
                    )

                return self._success(
                    {
                        "subscription_token": (
                            result.subscription_token
                        ),
                        "event_token": result.event_token,
                    }
                )
        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="query_subscribe",
            )

    async def _on_query_unsubscribe(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            payload = self._require_message(message)
            token = self._require_string(
                payload,
                "subscription_token",
            )
            result = self._require_proxy().unsubscribe_query(
                sid,
                token,
            )

            if result.should_leave_channel:
                await self._server.leave_room(
                    sid,
                    result.channel,
                )

            return self._success(True)
        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="query_unsubscribe",
            )

    async def _on_query_sync(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            async with self._mutation_lock:
                payload = self._require_message(message)
                token = self._require_string(
                    payload,
                    "subscription_token",
                )
                result = self._require_proxy().sync_query(
                    sid,
                    token,
                )
                return self._success(result)
        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="query_sync",
            )

    async def _on_query_audit(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            payload = self._require_message(message)
            values = payload.get("subscription_tokens")

            if not isinstance(values, list) or not all(
                isinstance(value, str) and value
                for value in values
            ):
                raise TypeError(
                    "Query subscription tokens must be strings"
                )

            result = self._require_proxy().audit_queries(
                sid,
                values,
            )
            return self._success(result)
        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="query_audit",
            )

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    async def _on_subscribe(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
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
                    "subscription_token": (
                        result.subscription_token
                    ),
                    "event_token": result.event_token,
                }
            )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="subscribe",
            )

    async def _on_unsubscribe(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
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
                operation="unsubscribe",
            )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    async def _on_signal_bind(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
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
                operation="signal_bind",
            )

    async def _on_signal_unbind(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
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
                operation="signal_unbind",
            )

    async def _on_signal_emit(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
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
                operation="signal_emit",
            )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    async def _on_transaction(
        self,
        sid: str,
        message: Any,
    ) -> dict[str, Any]:
        try:
            async with self._mutation_lock:
                payload = self._require_message(
                    message,
                )

                execution = self._require_proxy().transaction(
                    instructions=self._parse_instructions(
                        payload,
                    ),
                )

                if execution.result.success:
                    await self._emit_subscription_messages(
                        execution.dispatches,
                    )

                return self._execution_response(
                    execution.result,
                )

        except Exception as exc:
            return self._failure_from_exception(
                exc,
                operation="transaction",
            )

    async def _on_undo(
        self,
        sid: str,
        message: Any = None,
    ) -> dict[str, Any]:
        try:
            async with self._mutation_lock:
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
                operation="undo",
            )

    async def _on_redo(
        self,
        sid: str,
        message: Any = None,
    ) -> dict[str, Any]:
        try:
            async with self._mutation_lock:
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
                operation="redo",
            )

    # -------------------------------------------------
    # Event dispatch
    # -------------------------------------------------

    async def _emit_subscription_messages(
        self,
        messages: tuple[DispatchMessage, ...],
    ) -> None:
        if not messages:
            return

        messages_by_client: dict[
            str,
            list[dict[str, Any]],
        ] = defaultdict(list)

        for message in messages:
            participants = self._server.manager.get_participants(
                "/",
                message.channel,
            )

            for sid, _engineio_sid in participants:
                messages_by_client[sid].append(
                    message.payload,
                )

        for sid, payloads in messages_by_client.items():
            try:
                await self._server.emit(
                    "events",
                    {
                        "events": payloads,
                    },
                    to=sid,
                )
            except Exception:
                logger.exception(
                    "Could not emit store events to client %s; revision "
                    "auditing will reconcile the client.",
                    sid,
                )

    async def _emit_signal_messages(
        self,
        messages: tuple[SignalDispatchMessage, ...],
    ) -> None:
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
        if not isinstance(message, dict):
            raise TypeError(
                "Socket.IO request payload must be an object"
            )

        return message

    @staticmethod
    def _parse_address(
        message: dict[str, Any],
    ) -> Address[Any]:
        value = message.get(
            "address",
        )

        if not isinstance(value, list):
            raise TypeError(
                "Store address must be a list"
            )

        if not all(
            isinstance(part, str)
            for part in value
        ):
            raise TypeError(
                "Store address must contain only strings"
            )

        return Address(
            *value,
        )

    @staticmethod
    def _parse_addresses(
        message: dict[str, Any],
    ) -> list[Address[Any]]:
        value = message.get(
            "addresses",
        )

        if not isinstance(value, list):
            raise TypeError(
                "Store addresses must be a list"
            )

        addresses: list[Address[Any]] = []

        for parts in value:
            if not isinstance(parts, list) or not all(
                isinstance(part, str)
                for part in parts
            ):
                raise TypeError(
                    "Every store address must be a list of strings"
                )

            addresses.append(
                Address(*parts),
            )

        return addresses

    @staticmethod
    def _parse_opcode(
        message: dict[str, Any],
    ) -> OpCode:
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
        value = message.get(
            "fields",
        )

        if isinstance(value, str):
            if not value:
                raise TypeError(
                    "Lookup field must be non-empty"
                )

            return value

        if isinstance(value, list):
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
        value = message.get(
            key,
        )

        if (
            not isinstance(value, str)
            or not value
        ):
            raise TypeError(
                f"{key} must be a non-empty string"
            )

        return value

    @staticmethod
    def _parse_optional_string(
        message: dict[str, Any],
        key: str,
    ) -> str | None:
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
        value = message.get(
            key,
            default,
        )

        if not isinstance(value, bool):
            raise TypeError(
                f"{key} must be a boolean"
            )

        return value

    @staticmethod
    def _parse_depth(
        message: dict[str, Any],
    ) -> int | None:
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
        value = message.get(
            "instructions",
        )

        if not isinstance(value, list):
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
    ) -> dict[str, Any]:
        response = success(
            cls._serialize(
                data,
            )
        )

        return response.payload()

    @staticmethod
    def _failure(
        store_failure: StoreFailure,
    ) -> dict[str, Any]:
        return failure(
            store_failure,
        ).payload()

    @classmethod
    def _failure_from_exception(
        cls,
        exc: Exception,
        *,
        operation: str,
    ) -> dict[str, Any]:
        if isinstance(exc, StoreOperationError):
            return cls._failure(
                exc.failure(),
            )

        if isinstance(
            exc,
            (
                TypeError,
                ValueError,
                KeyError,
            ),
        ):
            request_error = InvalidInstructionError(
                str(exc) or "Invalid store request",
                detail=InstructionErrorDetail(
                    opcode=operation,
                ),
            )

            return cls._failure(
                request_error.failure(),
            )

        logger.exception(
            "Unexpected remote store request failure",
            extra={
                "operation": operation,
                "exception_type": type(exc).__name__,
            },
        )

        return cls._failure(
            StoreFailure(
                code=StoreErrorCode.INTERNAL_ERROR,
                category=StoreErrorCategory.INTERNAL,
                status=StoreErrorStatus.INTERNAL,
                message=(
                    "The remote store encountered an "
                    "unexpected internal error"
                ),
            )
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
            (
                str,
                int,
                float,
                bool,
            ),
        ):
            return value

        if isinstance(value, Address):
            return value.to_list()

        if isinstance(value, dict):
            return {
                str(key): cls._serialize(
                    item,
                )
                for key, item in value.items()
            }

        if isinstance(
            value,
            (
                list,
                tuple,
                set,
            ),
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
                    cast(Any, value),
                )
            )

        payload = getattr(
            value,
            "payload",
            None,
        )

        if callable(payload):
            return cls._serialize(
                payload(),
            )

        to_dict = getattr(
            value,
            "to_dict",
            None,
        )

        if callable(to_dict):
            return cls._serialize(
                to_dict(),
            )

        raise TypeError(
            "Value is not Socket.IO serializable: "
            f"{type(value).__name__}"
        )

    @classmethod
    def _execution_response(
        cls,
        result: ExecutionResult,
    ) -> dict[str, Any]:
        if result.success:
            return cls._success(
                result,
            )

        if result.error is None:
            raise RuntimeError(
                "Failed execution result has no error"
            )

        return cls._failure(
            result.error.failure(),
        )
