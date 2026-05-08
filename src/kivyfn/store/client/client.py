from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable

from kivyfn.store.address import Address
from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.events.event import StoreEvent
from kivyfn.store.events.subscription import Subscription
from kivyfn.store.transport import SocketIOClient
from kivyfn.store.transport.response import Response
from kivyfn.store.transaction import TransactionResult

if TYPE_CHECKING:
    from kivyfn.store import Store


class StoreClient:
    def __init__(self, store: Store, url: str) -> None:
        self.store = store
        self.url = url

        self._transport = SocketIOClient(
            url=url,
            event_hanlder=self.on_event,
        )

        # event_token -> local subscriptions using this event stream
        self._subscriptions: dict[str, set[Subscription]] = defaultdict(set)

        # subscription_token -> local subscription
        self._subscriptions_by_token: dict[str, Subscription] = {}

    async def connect(self) -> None:
        await self._transport.connect()

    async def disconnect(self) -> None:
        await self._transport.disconnect()
        self._subscriptions.clear()
        self._subscriptions_by_token.clear()

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
        propagate_foreignkeys: bool = False
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
                "propagate_foreignkeys": propagate_foreignkeys
            },
        )

        response = Response.from_payload(payload)
        if not response.ok:
            return response

        subscription_token = response.data["subscription_token"]
        event_token = response.data["event_token"]

        subscription = Subscription(
            store=self.store,
            client=self,
            token=subscription_token,
            event_token=event_token,
            callback=callback,
        )

        self._subscriptions[event_token].add(subscription)
        self._subscriptions_by_token[subscription_token] = subscription

        return response.copy_with(data=subscription)

    async def unsubscribe(self, token: str) -> Response[bool]:
        payload = await self._transport.call("unsubscribe", {"token": token})
        response = Response.from_payload(payload)

        if response.ok:
            subscription = self._subscriptions_by_token.pop(token, None)

            if subscription is not None:
                event_token = subscription.event_token
                bucket = self._subscriptions.get(event_token)

                if bucket is not None:
                    bucket.discard(subscription)

                    if not bucket:
                        self._subscriptions.pop(event_token, None)

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
        event_token: str = message["event_token"]
        event: dict[str, Any] = message["event"]

        self.dispatch(event_token, event)

    def dispatch(self, event_token: str, message: dict[str, Any]) -> None:
        subscriptions = self._subscriptions.get(event_token)
        if not subscriptions:
            return

        event = StoreEvent(**message)

        for subscription in tuple(subscriptions):
            subscription.dispatch(event)
