# kivyfn/store/client.py

from __future__ import annotations
from typing import Any, TYPE_CHECKING, Callable

from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.transport import SocketIOClient
from kivyfn.store.transport.response import Response
from kivyfn.store.events.subscription import Subscription
from kivyfn.store.events import NodeEvent
from kivyfn.store.transaction import TransactionResult

if TYPE_CHECKING:
    from kivyfn.store.address import Address


class StoreClient:
    def __init__(self, url: str) -> None:
        self.url = url

        self._transport = SocketIOClient(
            url=url,
            event_hanlder=self.on_event,
        )

        self._subscriptions: dict[str, Subscription] = {}

    async def connect(self) -> None:
        await self._transport.connect()

    async def disconnect(self) -> None:
        await self._transport.disconnect()
        self._subscriptions.clear()

    async def snapshot(
        self,
        address: Address,
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[Any]:
        payload = await self._transport.call(
            "snapshot",
            {
                "address": address.to_list(),
                "id": id,
                "key": key,
                "resolve_foreignkeys": resolve_foreignkeys,
            },
        )

        return Response.from_payload(payload)

    async def subscribe(
        self,
        opcode: InstructionOpCode,
        address: Address,
        callback: Callable,
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
    ) -> Response[Subscription]:
        payload = await self._transport.call(
            "subscribe",
            {
                "address": address.to_list(),
                "opcode": opcode.value,
                "id": id,
                "key": key,
                "depth": depth,
                "resolve_foreignkeys": resolve_foreignkeys,
            },
        )

        response = Response.from_payload(payload)
        if not response.ok:
            return response

        token = response.data["token"]

        subscription = Subscription(
            client=self,
            token=token,
            callback=callback,
        )

        self._subscriptions[token] = subscription
        return response.copy_with(data=subscription)

    async def unsubscribe(self, token: str) -> Response[bool]:
        payload = await self._transport.call("unsubscribe", {"token": token})
        response = Response.from_payload(payload)

        if response.ok:
            self._subscriptions.pop(token, None)

        return response

    async def transaction(self, instructions: list) -> Response[TransactionResult]:
        payload = await self._transport.call("transaction", instructions)
        response = Response.from_payload(payload)

        if not response.ok:
            return response

        result = TransactionResult.from_payload(response.data)
        return response.copy_with(data=result)

    async def undo(self) -> Response[Any]:
        payload = await self._transport.call("undo")
        return Response.from_payload(payload)

    async def redo(self) -> Response[Any]:
        payload = await self._transport.call("redo")
        return Response.from_payload(payload)

    async def to_dict(self, address: Address) -> Response[Any]:
        payload = await self._transport.call("to_dict", address.to_list())
        return Response.from_payload(payload)

    # -------------------------------------------------
    # Server Events
    # -------------------------------------------------
    def on_event(self, message: dict[str, Any]) -> None:
        token: str = message["token"]
        event: dict[str, Any] = message["event"]

        self.dispatch(token, event)

    def dispatch(self, token: str, message: dict[str, Any]) -> None:
        subscription = self._subscriptions.get(token)
        if subscription is None:
            return

        event = NodeEvent(**message)
        subscription.dispatch(event)
