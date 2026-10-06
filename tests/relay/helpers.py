from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import socketio
from aiohttp import web

from kivyfn.store.builder import (
    Branch,
    Builder,
    HistoryConfig,
    HostConfig,
    RelayConfig,
)
from kivyfn.store.codec.requests import encode_request
from kivyfn.store.host.host import StoreHost
from kivyfn.store.protocol.message.requests import (
    RequestBatch,
    RequestItem,
    SetRequestAction,
    TransactionRequest,
)
from kivyfn.store.protocol.version import PROTOCOL_VERSION
from kivyfn.store.schema import (
    ManySchema,
    NumericField,
    Signal,
    SingleSchema,
    StringField,
)
from kivyfn.store.store import Store
from kivyfn.store.transport.relay import SocketIORelay
from kivyfn.store.transport.relay.protocol import (
    ACK_EVENT,
    FRAME_EVENT,
    REGISTER_EVENT,
)


class RelayRoot(SingleSchema):
    value = NumericField(default=0, ge=0, le=100)
    ping = Signal()


class RelayRow(ManySchema):
    label = StringField(default="")
    value = NumericField(default=0)


def process_auth() -> dict[str, str]:
    return {"token": "test-only"}


class Gateway:
    """Wire-level test peer, not an application gateway implementation."""

    def __init__(self) -> None:
        self.sio = socketio.AsyncServer(async_mode="aiohttp")
        self.app = web.Application()
        self.sio.attach(self.app, socketio_path="relay/socket.io")
        self.sio.on("connect", self.connect)
        self.sio.on(REGISTER_EVENT, self.register)
        self.sio.on(FRAME_EVENT, self.receive)
        self.credentials: list[dict[str, Any]] = []
        self.registered: asyncio.Queue[tuple[str, dict[str, Any]]]
        self.registered = asyncio.Queue()
        self.frames: dict[tuple[str, int], asyncio.Queue[dict[str, Any]]]
        self.frames = defaultdict(asyncio.Queue)
        self.seen: list[dict[str, Any]] = []
        self.gates: dict[tuple[int, str], asyncio.Event] = {}
        self.reject_frames: set[tuple[int, str]] = set()
        self.accept = True
        self.registration_gate: asyncio.Event | None = None
        self.bad_registration = False
        self.runner = web.AppRunner(self.app)
        self.endpoint = ""

    async def start(self) -> None:
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        port = self.runner.addresses[0][1]
        self.endpoint = f"http://127.0.0.1:{port}/relay/socket.io"

    async def stop(self) -> None:
        for gate in self.gates.values():
            gate.set()
        if self.registration_gate is not None:
            self.registration_gate.set()
        await self.sio.shutdown()
        connections = tuple(self.sio.eio.sockets.values())
        await asyncio.gather(
            *(connection.close(wait=False) for connection in connections)
        )
        for connection in connections:
            connection.queue.put_nowait(None)
        self.sio.eio.sockets.clear()
        await self.runner.cleanup()

    async def connect(
        self, sid: str, environ: dict[str, Any], auth: dict[str, Any]
    ) -> bool:
        self.credentials.append(auth)
        return self.accept

    async def register(
        self, sid: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        await self.registered.put((sid, payload))
        if self.registration_gate is not None:
            await self.registration_gate.wait()
        return {
            **payload,
            "accepted": True,
            "relay_version": 999 if self.bad_registration else 1,
        }

    async def receive(self, sid: str, frame: dict[str, Any]) -> None:
        self.seen.append(frame)
        self.frames[(sid, frame["channel_id"])].put_nowait(frame)
        key = (frame["channel_id"], frame["type"])
        gate = self.gates.get(key)
        if gate is not None:
            await gate.wait()
        await self.sio.emit(
            ACK_EVENT,
            {
                "generation": frame["generation"],
                "channel_id": frame["channel_id"],
                "sequence": frame["sequence"],
                "accepted": key not in self.reject_frames,
            },
            to=sid,
        )

    async def registration(self) -> tuple[str, dict[str, Any]]:
        async with asyncio.timeout(3):
            return await self.registered.get()

    async def send(self, sid: str, frame: object) -> dict[str, Any]:
        receipt = await self.sio.call(FRAME_EVENT, frame, to=sid, timeout=2)
        assert isinstance(receipt, dict)
        return receipt

    async def next_frame(self, sid: str, channel: int) -> dict[str, Any]:
        async with asyncio.timeout(3):
            return await self.frames[(sid, channel)].get()


class Peer:
    def __init__(
        self, gateway: Gateway, sid: str, generation: str, number: int
    ) -> None:
        self.gateway = gateway
        self.sid = sid
        self.generation = generation
        self.number = number
        self.sequence = 0
        self.prefix = ""

    async def open(self) -> None:
        receipt = await self.send(
            "open", {"protocol_version": PROTOCOL_VERSION}
        )
        assert receipt == {"accepted": True}
        frame = await self.next()
        assert frame["type"] == "opened"
        assert frame["sequence"] == 0
        self.prefix = frame["payload"]["transaction_prefix"]

    async def send(
        self, kind: str, payload: object = None, **changes: Any
    ) -> dict[str, Any]:
        frame = {
            "type": kind,
            "generation": self.generation,
            "channel_id": self.number,
            "sequence": self.sequence,
            "payload": payload,
            **changes,
        }
        self.sequence += 1
        return await self.gateway.send(self.sid, frame)

    async def submit(self, *items: RequestItem) -> dict[str, Any]:
        return await self.send("request", encode_request(RequestBatch(items)))

    async def response(self, *items: RequestItem) -> dict[str, Any]:
        sequence = self.sequence
        assert await self.submit(*items) == {"accepted": True}
        while True:
            frame = await self.next()
            if frame["type"] == "response":
                assert frame["request_sequence"] == sequence
                return frame["payload"]
            assert frame["type"] == "event"

    async def next(self) -> dict[str, Any]:
        return await self.gateway.next_frame(self.sid, self.number)

    def transaction(
        self, value: int, *, nonce: str = ""
    ) -> TransactionRequest:
        return TransactionRequest(
            self.prefix + (nonce or uuid4().hex),
            (SetRequestAction("RelayRoot", (("value", value),)),),
        )


@dataclass
class Stack:
    store: Store
    host: StoreHost
    gateway: Gateway
    sid: str
    generation: str

    @property
    def relay(self) -> SocketIORelay:
        assert self.host.relay is not None
        return self.host.relay

    async def peer(self, number: int) -> Peer:
        peer = Peer(self.gateway, self.sid, self.generation, number)
        await peer.open()
        return peer


@asynccontextmanager
async def running_stack(
    path: Path, gateway: Gateway, **config: Any
) -> AsyncIterator[Stack]:
    options = {
        "endpoint": gateway.endpoint,
        "store_id": "application-001",
        "auth": process_auth,
        "reconnect_interval": 0.02,
        "connect_timeout": 0.5,
        "request_timeout": 0.5,
        **config,
    }
    tree = Branch(RelayRoot).child("rows", Branch(RelayRow))
    store = Store(
        Builder()
        .tree(tree)
        .storage_path(path)
        .history(HistoryConfig())
        .host(
            HostConfig(f"direct://{uuid4().hex}", relay=RelayConfig(**options))
        )
        .build()
    )
    host = StoreHost(store)
    store.boot()
    try:
        await host.start()
        sid, registration = await gateway.registration()
        assert host.relay is not None
        await host.relay.wait_online(timeout=3)
        yield Stack(store, host, gateway, sid, registration["generation"])
    finally:
        await host.stop()
        store.shutdown()
