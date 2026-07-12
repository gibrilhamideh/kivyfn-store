# kivyfn/store/transport/socketio_server.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

import socketio
from aiohttp import web

from kivyfn.logging import get_logger
from kivyfn.store.core.engine import Engine
from kivyfn.store.errors.store import StoreError
from kivyfn.store.transport import response


if TYPE_CHECKING:
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.node import Node


logger = get_logger(__name__)


class SocketIOServer:
    def __init__(
        self,
        root: Node,
        config: StoreConfig,
        host: str,
        port: int,
        on_ready: Callable[[], None],
    ) -> None:
        self.host = host
        self.port = port
        self.on_ready = on_ready

        self.engine = Engine(
            self,
            root,
            config,
        )

        self.sio = socketio.AsyncServer(
            async_mode="aiohttp",
            cors_allowed_origins=[],
            logger=False,
            engineio_logger=False,
        )

        self.app = web.Application()

        self.sio.attach(
            self.app,
        )

        self._register_handlers()

    async def run(self) -> None:
        self.engine.boot()

        runner = web.AppRunner(
            self.app,
        )

        await runner.setup()

        site = web.TCPSite(
            runner,
            self.host,
            self.port,
        )

        await site.start()

        self.on_ready()

        await asyncio.Event().wait()

    # -------------------------------------------------
    # Handler registration
    # -------------------------------------------------

    def _register_handlers(self) -> None:
        self.sio.on(
            "connect",
            self.on_connect,
        )
        self.sio.on(
            "disconnect",
            self.on_disconnect,
        )

        self.sio.on(
            "undo",
            self.on_undo,
        )
        self.sio.on(
            "redo",
            self.on_redo,
        )
        self.sio.on(
            "snapshot",
            self.on_snapshot,
        )
        self.sio.on(
            "lookup",
            self.on_lookup,
        )
        self.sio.on(
            "subscribe",
            self.on_subscribe,
        )
        self.sio.on(
            "unsubscribe",
            self.on_unsubscribe,
        )
        self.sio.on(
            "transaction",
            self.on_transaction,
        )

        self.sio.on(
            "signal_bind",
            self.on_signal_bind,
        )
        self.sio.on(
            "signal_unbind",
            self.on_signal_unbind,
        )
        self.sio.on(
            "signal_emit",
            self.on_signal_emit,
        )

    # -------------------------------------------------
    # Client lifecycle
    # -------------------------------------------------

    async def on_connect(
        self,
        sid: str,
        environ: dict[str, Any],
    ) -> None:
        pass

    async def on_disconnect(
        self,
        sid: str,
    ) -> None:
        self.engine.remove_sid(
            sid,
        )

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------
    async def on_lookup(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            data = self.engine.lookup(
                payload,
            )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled lookup error: sid=%s payload=%r",
                sid,
                payload,
            )

            return response.failure(
                status=500,
                error={
                    "type": type(exc).__name__,
                    "code": "internal_store_error",
                    "message": str(exc),
                },
            ).payload()

        return response.success(
            data=data,
            status=200,
        ).payload()

    async def on_snapshot(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            data = self.engine.snapshot(
                payload,
            )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled snapshot error: sid=%s payload=%r",
                sid,
                payload,
            )

            return self._internal_error(
                exc,
            )

        return response.success(
            data=data,
            status=200,
        ).payload()

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    async def on_subscribe(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = self.engine.subscribe(
                sid,
                payload,
            )

            if result.should_join_room:
                await self.sio.enter_room(
                    sid=sid,
                    room=result.room,
                )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled subscribe error: sid=%s payload=%r",
                sid,
                payload,
            )

            return self._internal_error(
                exc,
            )

        return response.success(
            data={
                "subscription_token": result.subscription_token,
                "event_token": result.event_token,
            },
            status=201,
        ).payload()

    async def on_unsubscribe(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = self.engine.unsubscribe(
                sid,
                payload,
            )

            if result.should_leave_room:
                await self.sio.leave_room(
                    sid=sid,
                    room=result.room,
                )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled unsubscribe error: sid=%s payload=%r",
                sid,
                payload,
            )

            return self._internal_error(
                exc,
            )

        return response.success(
            data=True,
            status=200,
        ).payload()

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    async def on_signal_bind(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = self.engine.bind_signal(
                sid,
                payload,
            )

            if result.should_join_room:
                await self.sio.enter_room(
                    sid=sid,
                    room=result.room,
                )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled signal bind error: sid=%s payload=%r",
                sid,
                payload,
            )

            return self._internal_error(
                exc,
            )

        return response.success(
            data={
                "binding_token": result.binding_token,
                "event_token": result.event_token,
            },
            status=201,
        ).payload()

    async def on_signal_unbind(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = self.engine.unbind_signal(
                sid,
                payload,
            )

            if result.should_leave_room:
                await self.sio.leave_room(
                    sid=sid,
                    room=result.room,
                )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled signal unbind error: sid=%s payload=%r",
                sid,
                payload,
            )

            return self._internal_error(
                exc,
            )

        return response.success(
            data=True,
            status=200,
        ).payload()

    async def on_signal_emit(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            messages = self.engine.emit_signal(
                payload,
            )

            for message in messages:
                await self.send_signal(
                    room=message.room,
                    payload=message.payload,
                )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled signal emit error: sid=%s payload=%r",
                sid,
                payload,
            )

            return self._internal_error(
                exc,
            )

        return response.success(
            data=True,
            status=200,
        ).payload()

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    async def on_transaction(
        self,
        sid: str,
        commands: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            result, messages = self.engine.execute(
                commands,
            )

            if not result.success:
                error = result.error

                if error is None:
                    raise RuntimeError(
                        "Transaction failed without an error"
                    )

                return response.failure(
                    status=error.status,
                    error=error.payload(),
                ).payload()

            for message in messages:
                await self.send_event(
                    room=message.room,
                    payload=message.payload,
                )

        except StoreError as exc:
            return response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        except Exception as exc:
            logger.exception(
                "Unhandled transaction error: sid=%s commands=%r",
                sid,
                commands,
            )

            return self._internal_error(
                exc,
            )

        return response.success(
            data=result.payload(),
            status=200,
        ).payload()

    # -------------------------------------------------
    # Undo / redo
    # -------------------------------------------------

    async def on_undo(
        self,
        sid: str,
        *_: Any,
    ) -> dict[str, Any]:
        return response.failure(
            status=501,
            error={
                "type": "NotImplementedError",
                "code": "not_implemented",
                "message": "Undo is not implemented",
            },
        ).payload()

    async def on_redo(
        self,
        sid: str,
        *_: Any,
    ) -> dict[str, Any]:
        return response.failure(
            status=501,
            error={
                "type": "NotImplementedError",
                "code": "not_implemented",
                "message": "Redo is not implemented",
            },
        ).payload()

    # -------------------------------------------------
    # Outgoing messages
    # -------------------------------------------------

    async def send_event(
        self,
        room: str,
        payload: dict[str, Any],
    ) -> None:
        await self.sio.emit(
            "event",
            payload,
            room=room,
        )

    async def send_signal(
        self,
        room: str,
        payload: dict[str, Any],
    ) -> None:
        await self.sio.emit(
            "signal",
            payload,
            room=room,
        )

    # -------------------------------------------------
    # Errors
    # -------------------------------------------------

    def _internal_error(
        self,
        exc: Exception,
    ) -> dict[str, Any]:
        return response.failure(
            status=500,
            error={
                "type": type(exc).__name__,
                "code": "internal_store_error",
                "message": str(exc),
            },
        ).payload()
