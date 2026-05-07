from __future__ import annotations
from typing import Any, TYPE_CHECKING, Callable

import asyncio
import socketio
from aiohttp import web
from collections import defaultdict

from kivyfn.store.address import Address
from kivyfn.store.core import Engine
from kivyfn.store.core.execution import Execution
from kivyfn.store.events import NodeEvent
from kivyfn.store.events.subscription import EngineSubscription
from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.transport.response import Response

if TYPE_CHECKING:
    from kivyfn.store.node import Node
    from kivyfn.store.events.listener import Listener
    from kivyfn.store.builder.config import StoreConfig


class SocketIOServer:
    def __init__(
        self, root: Node, config: StoreConfig, host: str, port: int, on_ready: Callable
    ) -> None:
        self.host = host
        self.port = port
        self.on_ready = on_ready

        self._engine = Engine(self, root, config)
        self._subscriptions: dict[str, EngineSubscription] = {}
        self._listeners: dict[Listener, set[EngineSubscription]] = defaultdict(set)

        self.sio = socketio.AsyncServer(
            async_mode="aiohttp",
            cors_allowed_origins=[],
            logger=False,
            engineio_logger=False,
        )

        self.app = web.Application()
        self.sio.attach(self.app)
        self._register_handlers()

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def run(self) -> None:
        self._engine.boot()

        runner = web.AppRunner(self.app)
        await runner.setup()

        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

        self.on_ready()

        await asyncio.Future()

    # -------------------------------------------------
    # Handler Registration (direct mapping)
    # -------------------------------------------------

    def _register_handlers(self) -> None:
        self.sio.on("connect", self.on_connect)
        self.sio.on("disconnect", self.on_disconnect)

        # RPC-style handlers
        self.sio.on("undo", self.on_undo)
        self.sio.on("redo", self.on_redo)
        self.sio.on("snapshot", self.on_snapshot)
        self.sio.on("subscribe", self.on_subscribe)
        self.sio.on("unsubscribe", self.on_unsubscribe)
        self.sio.on("transaction", self.on_transaction)

    # -------------------------------------------------
    # Core Events
    # -------------------------------------------------

    async def on_connect(self, sid: str, environ: dict[str, Any]) -> None:
        pass

    async def on_disconnect(self, sid: str) -> None:
        pass

    # -------------------------------------------------
    # Operations (direct handlers)
    # -------------------------------------------------
    async def on_snapshot(
        self,
        sid: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            address = Address(*message["address"])
            node = self._engine.resolve(address)

            data = node.snapshot(
                id=message.get("id"),
                key=message.get("key"),
                resolve_foreignkeys=message.get("resolve_foreignkeys", False),
            )

            return Response.success(
                data=data,
                status=200,
            ).payload()

        except Exception as e:
            return Response.error(
                message=str(e),
                status=400,
            ).payload()

    async def on_unsubscribe(
        self,
        sid: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            token = message["token"]

            subscription = self._subscriptions.pop(token, None)
            if subscription is None:
                return Response.error(
                    message="Subscription not found",
                    status=404,
                ).payload()

            listener = subscription.listener
            room = listener.room

            subscriptions = self._listeners.get(listener)
            if subscriptions is not None:
                subscriptions.discard(subscription)

                if not subscriptions:
                    self._listeners.pop(listener, None)
                    subscription.dispose()

            await self.sio.leave_room(sid=sid, room=room)

            return Response.success(
                data=True,
                status=200,
            ).payload()

        except Exception as e:
            return Response.error(
                message=str(e),
                status=400,
            ).payload()

    async def on_subscribe(
        self,
        sid: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            opcode = InstructionOpCode(message["opcode"])
            address = Address(*message["address"])
            node = self._engine.resolve(address)

            listener = node.subscribe(
                opcode=opcode,
                id=message.get("id", None),
                key=message.get("key", None),
                depth=message.get("depth", 0),
                resolve_foreignkeys=message.get("resolve_foreignkeys", False),
            )

            subscription = EngineSubscription(node, listener)
            token = subscription.token

            self._subscriptions[token] = subscription
            self._listeners[listener].add(subscription)

            await self.sio.enter_room(sid=sid, room=listener.room)

            return Response.success(
                data={"token": token},
                status=201,
            ).payload()

        except Exception as e:
            return Response.error(
                message=str(e),
                status=400,
            ).payload()

    async def on_transaction(
        self,
        sid: str,
        commands: list,
    ) -> dict[str, Any]:
        try:
            transaction = Execution(self._engine, commands)
            execution = transaction.execute()

            self._engine.to_disk(execution.touched_nodes)
            for event in execution.events:
                node = self._engine.resolve(event.address)
                listeners = node.dispatch(event)
                await self.dispatch(event, listeners)

            return Response.success(
                data=execution.payload(),
                status=200,
            ).payload()

        except Exception as e:
            print(e)
            return Response.error(
                message=str(e),
                status=500,
            ).payload()

    async def on_undo(self, sid, *_):
        pass

    async def on_redo(self, sid, *_):
        pass

    # -------------------------------------------------
    # Server Push
    # -------------------------------------------------
    async def send(self, room: str, event: dict[str, Any]) -> None:
        await self.sio.emit("event", event, room=room)

    async def dispatch(self, event: NodeEvent, listeners: list[Listener]):
        _event = event.to_dict()
        for listener in listeners:
            subscriptions = self._listeners.get(listener)
            if subscriptions is None:
                continue
            for subscription in subscriptions:
                message = {"token": subscription.token, "event": _event}
                await self.send(listener.room, message)
