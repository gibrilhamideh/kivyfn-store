from __future__ import annotations

import asyncio
from math import ceil
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import socketio

from kivyfn.store.error import StoreConnectionError, StoreProtocolError
from kivyfn.store.protocol.version import PROTOCOL_VERSION
from kivyfn.store.transport.relay.channel import RelayChannel
from kivyfn.store.transport.relay.protocol import (
    ACK_EVENT,
    FRAME_EVENT,
    REGISTER_EVENT,
    RELAY_VERSION,
    decode_frame,
    frame_size,
)

if TYPE_CHECKING:
    from kivyfn.store.host.host import StoreHost
    from kivyfn.store.transport.relay.adapter import SocketIORelay


class RelaySession:
    """One physical connection, never reused after a disconnect."""

    def __init__(self, relay: SocketIORelay, host: StoreHost) -> None:
        self.relay = relay
        self.host = host
        self.config = relay.config
        self.generation = uuid4().hex
        self.ready = asyncio.Event()
        self.lost = asyncio.Event()
        self.error: Exception | None = None
        self.channels: dict[int, RelayChannel] = {}
        self._last_channel = 0
        self._writes = asyncio.Lock()
        self._pending: dict[tuple[int, int], asyncio.Future[bool]] = {}
        self.sio = socketio.AsyncClient(
            reconnection=False,
            handle_sigint=False,
            request_timeout=self.config.connect_timeout,
        )
        self.sio.on("disconnect", self._on_disconnect)
        self.sio.on(FRAME_EVENT, self._receive)
        self.sio.on(ACK_EVENT, self._acknowledge)

    async def connect(self, credentials: dict[str, Any]) -> None:
        registration = {
            "store_id": self.config.store_id,
            "generation": self.generation,
            "relay_version": RELAY_VERSION,
            "protocol_version": PROTOCOL_VERSION,
        }
        auth = {**registration, "role": "store", "credentials": credentials}
        frame_size(auth, self.config.max_frame_bytes)
        await self.sio.connect(
            self.relay.url,
            auth=auth,
            socketio_path=self.relay.socketio_path,
            wait_timeout=ceil(self.config.connect_timeout),
        )
        reply = await self.sio.call(
            REGISTER_EVENT,
            registration,
            timeout=ceil(self.config.connect_timeout),
        )
        if (
            not isinstance(reply, dict)
            or reply.get("accepted") is not True
            or reply.get("generation") != self.generation
            or type(reply.get("relay_version")) is not int
            or reply.get("relay_version") != RELAY_VERSION
            or type(reply.get("protocol_version")) is not int
            or reply.get("protocol_version") != PROTOCOL_VERSION
        ):
            raise StoreProtocolError("Incompatible relay registration")
        if self.lost.is_set():
            raise StoreConnectionError("Relay connection was lost")

    def lose(self, message: str) -> None:
        if not self.lost.is_set():
            self.error = StoreConnectionError(message)
            self.lost.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_result(False)
            self.relay._notify()

    def _on_disconnect(self, reason: object = None) -> None:
        self.lose("Relay connection was lost")

    def _receive(self, *values: object) -> dict[str, Any]:
        if self.lost.is_set():
            return {"accepted": False, "code": "disconnected"}
        try:
            value = values[0] if len(values) == 1 else values
            size = frame_size(value, self.config.max_frame_bytes)
            frame = decode_frame(value)
        except ValueError:
            self.lose("Invalid relay envelope")
            return {"accepted": False, "code": "invalid_frame"}
        if frame["generation"] != self.generation:
            return {"accepted": False, "code": "stale_generation"}
        number = frame["channel_id"]
        channel = self.channels.get(number)
        if frame["type"] == "open":
            if number <= self._last_channel or frame["sequence"] != 0:
                if channel is not None:
                    channel.close("invalid_sequence")
                return {"accepted": False, "code": "invalid_sequence"}
            self._last_channel = number
            if len(self.channels) >= self.config.max_clients:
                return {"accepted": False, "code": "client_limit"}
            payload = frame.get("payload")
            if (
                not isinstance(payload, dict)
                or type(payload.get("protocol_version")) is not int
                or payload.get("protocol_version") != PROTOCOL_VERSION
            ):
                return {"accepted": False, "code": "protocol_mismatch"}
            self.channels[number] = RelayChannel(self, number)
            return {"accepted": True}
        if channel is None or channel.closing:
            return {"accepted": False, "code": "unknown_channel"}
        if frame["sequence"] != channel.in_sequence + 1:
            channel.close("invalid_sequence")
            return {"accepted": False, "code": "invalid_sequence"}
        channel.in_sequence = frame["sequence"]
        if frame["type"] == "close":
            channel.close("client_disconnected")
        elif not channel.enqueue(frame, size):
            channel.close("queue_full")
            return {"accepted": False, "code": "queue_full"}
        return {"accepted": True}

    async def send(self, frame: dict[str, Any]) -> None:
        """Await a gateway *receipt*, used only for bounded flow control."""
        frame_size(frame, self.config.max_frame_bytes)
        if self.lost.is_set():
            raise StoreConnectionError("Relay connection was lost")
        key = (frame["channel_id"], frame["sequence"])
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            async with asyncio.timeout(self.config.request_timeout):
                # Hold the physical writer only while enqueueing a packet.
                # An unresponsive logical client cannot block other writers.
                async with self._writes:
                    await self.sio.emit(FRAME_EVENT, frame)
                accepted = await future
            if not accepted:
                raise StoreConnectionError("Gateway rejected relay delivery")
        finally:
            self._pending.pop(key, None)

    def _acknowledge(self, *values: object) -> None:
        value = values[0] if len(values) == 1 else None
        if not isinstance(value, dict):
            return
        if value.get("generation") != self.generation:
            return
        channel = value.get("channel_id")
        sequence = value.get("sequence")
        if type(channel) is not int or type(sequence) is not int:
            return
        future = self._pending.get((channel, sequence))
        if future is not None and not future.done():
            future.set_result(value.get("accepted") is True)

    async def close(self) -> None:
        self.lose("Relay connection closed")
        channels = tuple(self.channels.values())
        await asyncio.gather(
            *(
                channel.close("disconnected", notify=False)
                for channel in channels
            )
        )
        self._pending.clear()
        eio = self.sio.eio
        tasks = tuple(
            task
            for task in (eio.read_loop_task, eio.write_loop_task)
            if task is not None
        )
        disconnecting = asyncio.create_task(self.sio.disconnect())
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                writer = eio.write_loop_task
                finished = writer if writer is not None else disconnecting
                await asyncio.shield(finished)
        except TimeoutError:
            pass
        finally:
            disconnecting.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(disconnecting, *tasks, return_exceptions=True)
            await eio.disconnect(abort=True)
            self.sio.callbacks.clear()
