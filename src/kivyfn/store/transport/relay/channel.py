from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kivyfn.store.codec.events import encode_event_message
from kivyfn.store.codec.requests import decode_request
from kivyfn.store.codec.responses import encode_response
from kivyfn.store.error import StoreConnectionError
from kivyfn.store.protocol.message.event import EventMessage
from kivyfn.store.protocol.message.requests import (
    RedoRequest,
    TransactionRequest,
    UndoRequest,
)
from kivyfn.store.protocol.version import PROTOCOL_VERSION
from kivyfn.store.transport.relay.protocol import MAX_SEQUENCE

if TYPE_CHECKING:
    from kivyfn.store.transport.relay.session import RelaySession


class RelayChannel:
    """One remote client: one request worker and one ordered output stream."""

    def __init__(self, session: RelaySession, number: int) -> None:
        self.session = session
        self.number = number
        self.client_id = f"relay:{session.generation}:{number}"
        self.transaction_prefix = self.client_id + ":"
        self.in_sequence = 0
        self._out_sequence = 0
        self._queue: asyncio.Queue[tuple[dict[str, Any], int]] = (
            asyncio.Queue()
        )
        self._pending_bytes = 0
        self._writes = asyncio.Lock()
        self._closing: asyncio.Task[None] | None = None
        self._task = asyncio.create_task(self._run(), name=self.client_id)

    @property
    def closing(self) -> bool:
        return self._closing is not None

    def enqueue(self, frame: dict[str, Any], size: int) -> bool:
        config = self.session.config
        if (
            self._queue.qsize() >= config.max_pending_messages
            or self._pending_bytes + size > config.max_pending_bytes
        ):
            return False
        self._pending_bytes += size
        self._queue.put_nowait((frame, size))
        return True

    async def _run(self) -> None:
        try:
            # The server may enqueue open immediately after its register ACK.
            await self.session.ready.wait()
            if self.session.lost.is_set():
                return
            await self.session.host.connect(
                self.client_id,
                send=self._event,
                close=self._host_closed,
                protocol_version=PROTOCOL_VERSION,
                max_pending=self.session.config.max_pending_messages,
            )
            await self._send(
                "opened",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "transaction_prefix": self.transaction_prefix,
                },
            )
            while not self.session.lost.is_set():
                frame, size = await self._queue.get()
                self._pending_bytes -= size
                if self.session.lost.is_set() or self.closing:
                    return
                try:
                    request = decode_request(frame.get("payload"))
                    for item in request.items:
                        if isinstance(
                            item,
                            (TransactionRequest, UndoRequest, RedoRequest),
                        ) and not item.transaction_id.startswith(
                            self.transaction_prefix
                        ):
                            raise ValueError("Invalid transaction namespace")
                except (TypeError, ValueError, RecursionError):
                    self.close("invalid_request")
                    return
                response = await self.session.host.request(
                    self.client_id, request
                )
                await self._send(
                    "response",
                    encode_response(response),
                    request_sequence=frame["sequence"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.close("delivery_failed")

    async def _event(self, client_id: str, message: EventMessage) -> None:
        await self._send("event", encode_event_message(message))

    async def _send(
        self, kind: str, payload: dict[str, Any], **metadata: Any
    ) -> None:
        async with self._writes:
            if self._out_sequence > MAX_SEQUENCE:
                raise StoreConnectionError("Relay sequence exhausted")
            if self.closing and kind != "closed":
                raise StoreConnectionError("Relay channel is closed")
            frame = {
                "generation": self.session.generation,
                "channel_id": self.number,
                "sequence": self._out_sequence,
                "type": kind,
                "payload": payload,
                **metadata,
            }
            self._out_sequence += 1
            await self.session.send(frame)

    async def _host_closed(self, client_id: str) -> None:
        await asyncio.shield(self.close("delivery_failed"))

    def close(self, code: str, *, notify: bool = True) -> asyncio.Task[None]:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(code, notify))
        return self._closing

    async def _close(self, code: str, notify: bool) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        try:
            await self.session.host.disconnect(self.client_id)
            if notify and not self.session.lost.is_set():
                try:
                    await self._send("closed", {"code": code})
                except Exception:
                    # Without a close receipt, the gateway may still route
                    # this channel. Reset the physical session to fence it.
                    self.session.lose("Relay channel close was not received")
        finally:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._pending_bytes = 0
            self.session.channels.pop(self.number, None)
