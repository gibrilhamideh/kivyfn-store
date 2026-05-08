from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

import socketio
from aiohttp import web

from kivyfn.store.core.engine import Engine
from kivyfn.store.errors.store import StoreError
from kivyfn.store.transport.response import Response

if TYPE_CHECKING:
    from kivyfn.store.builder.config import StoreConfig
    from kivyfn.store.node import Node


class SocketIOServer:
    def __init__(
        self,
        root: Node,
        config: StoreConfig,
        host: str,
        port: int,
        on_ready: Callable,
    ) -> None:
        self.host = host
        self.port = port
        self.on_ready = on_ready

        self.engine = Engine(self, root, config)

        self.sio = socketio.AsyncServer(
            async_mode="aiohttp",
            cors_allowed_origins=[],
            logger=False,
            engineio_logger=False,
        )

        self.app = web.Application()
        self.sio.attach(self.app)
        self._register_handlers()

    async def run(self) -> None:
        self.engine.boot()

        runner = web.AppRunner(self.app)
        await runner.setup()

        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

        self.on_ready()

        await asyncio.Future()

    def _register_handlers(self) -> None:
        self.sio.on("connect", self.on_connect)
        self.sio.on("disconnect", self.on_disconnect)

        self.sio.on("undo", self.on_undo)
        self.sio.on("redo", self.on_redo)
        self.sio.on("snapshot", self.on_snapshot)
        self.sio.on("subscribe", self.on_subscribe)
        self.sio.on("unsubscribe", self.on_unsubscribe)
        self.sio.on("transaction", self.on_transaction)

    async def on_connect(self, sid: str, environ: dict[str, Any]) -> None:
        pass

    async def on_disconnect(self, sid: str) -> None:
        self.engine.remove_sid(sid)

    async def on_snapshot(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            data = self.engine.snapshot(payload)
        except StoreError as exc:
            return Response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        return Response.success(data=data, status=200).payload()

    async def on_subscribe(
        self,
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = self.engine.subscribe(sid, payload)

            if result.should_join_room:
                await self.sio.enter_room(
                    sid=sid,
                    room=result.room,
                )

        except StoreError as exc:
            return Response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        return Response.success(
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
            result = self.engine.unsubscribe(sid, payload)
            if result.should_leave_room:
                await self.sio.leave_room(
                    sid=sid,
                    room=result.room,
                )

        except StoreError as exc:
            return Response.failure(
                status=exc.status,
                error=exc.payload(),
            ).payload()

        return Response.success(data=True, status=200).payload()

    async def on_transaction(
        self,
        sid: str,
        commands: list,
    ) -> dict[str, Any]:
        result, messages = self.engine.execute(commands)

        for message in messages:
            await self.send(
                room=message.room,
                payload=message.payload,
            )

        if not result.success:
            return Response.failure(
                status=result.error.status,
                error=result.error.payload(),
            ).payload()

        return Response.success(
            data=result.payload(),
            status=200,
        ).payload()

    async def on_undo(self, sid: str, *_):
        pass

    async def on_redo(self, sid: str, *_):
        pass

    async def send(self, room: str, payload: dict[str, Any]) -> None:
        await self.sio.emit(
            "event",
            payload,
            room=room,
        )
