# kivyfn/store/client/client.py
from __future__ import annotations

from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    TypeVar,
    overload,
)

from kivyfn.store.address import Address
from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
    event_from_payload,
)
from kivyfn.store.events.subscription import Subscription
from kivyfn.store.transaction import TransactionResult
from kivyfn.store.transport import SocketIOClient, response
from kivyfn.store.transport.response import Response


if TYPE_CHECKING:
    from kivyfn.store import Store


T = TypeVar("T")


class StoreClient:
    def __init__(
        self,
        store: Store,
        url: str,
    ) -> None:
        self.store = store
        self.url = url

        self._transport = SocketIOClient(
            url=url,
            event_hanlder=self.on_event,
        )

        # event_token -> local subscriptions using this event stream
        self._subscriptions: dict[
            str,
            set[Subscription[Any]],
        ] = defaultdict(set)

        # subscription_token -> local subscription
        self._subscriptions_by_token: dict[
            str,
            Subscription[Any],
        ] = {}

    async def connect(self) -> None:
        await self._transport.connect()

    async def disconnect(self) -> None:
        await self._transport.disconnect()

        self._subscriptions.clear()
        self._subscriptions_by_token.clear()

    async def snapshot(
        self,
        address: Address[T],
        id: str | None = None,
        key: str | None = None,
        *,
        resolve_foreignkeys: bool = False,
    ) -> Response[T]:
        payload = await self._transport.call(
            "snapshot",
            {
                "address": address.to_list(),
                "id": id,
                "key": key,
                "resolve_foreignkeys": resolve_foreignkeys,
            },
        )

        return response.from_payload(payload)

    # -------------------------------------------------
    # Subscriptions
    # -------------------------------------------------

    @overload
    async def subscribe(
        self,
        opcode: Literal[InstructionOpCode.SET],
        address: Address[Any],
        callback: Callable[[SetEvent], None],
        *,
        id: None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[Subscription[SetEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[InstructionOpCode.SETROW],
        address: Address[Any],
        callback: Callable[[SetRowEvent], None],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[Subscription[SetRowEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[InstructionOpCode.INSERT],
        address: Address[Any],
        callback: Callable[[InsertEvent[T]], None],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[Subscription[InsertEvent[T]]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[InstructionOpCode.REMOVE],
        address: Address[Any],
        callback: Callable[[RemoveEvent[T]], None],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[Subscription[RemoveEvent[T]]]:
        ...

    async def subscribe(
        self,
        opcode: InstructionOpCode,
        address: Address[Any],
        callback: Callable[[Any], None],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[Subscription[Any]]:
        payload = await self._transport.call(
            "subscribe",
            {
                "address": address.to_list(),
                "opcode": opcode.value,
                "id": id,
                "key": key,
                "depth": depth,
                "resolve_foreignkeys": resolve_foreignkeys,
                "propagate_foreignkeys": propagate_foreignkeys,
            },
        )

        result = response.from_payload(payload)

        if result.ok is False:
            return result

        subscription_token = result.data["subscription_token"]
        event_token = result.data["event_token"]

        subscription: Subscription[Any] = Subscription(
            store=self.store,
            client=self,
            token=subscription_token,
            event_token=event_token,
            callback=callback,
        )

        self._subscriptions[event_token].add(
            subscription,
        )

        self._subscriptions_by_token[
            subscription_token
        ] = subscription

        return response.success(
            data=subscription,
            status=result.status,
        )

    async def unsubscribe(
        self,
        token: str,
    ) -> Response[bool]:
        payload = await self._transport.call(
            "unsubscribe",
            {
                "token": token,
            },
        )

        result = response.from_payload(payload)

        if result.ok is False:
            return result

        subscription = self._subscriptions_by_token.pop(
            token,
            None,
        )

        if subscription is not None:
            event_token = subscription.event_token
            bucket = self._subscriptions.get(
                event_token,
            )

            if bucket is not None:
                bucket.discard(
                    subscription,
                )

                if not bucket:
                    self._subscriptions.pop(
                        event_token,
                        None,
                    )

        return result

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    async def transaction(
        self,
        instructions: list[dict[str, Any]],
    ) -> Response[TransactionResult]:
        payload = await self._transport.call(
            "transaction",
            instructions,
        )

        result = response.from_payload(payload)

        if result.ok is False:
            return result

        transaction_result = TransactionResult.from_payload(
            result.data,
        )

        return response.success(
            data=transaction_result,
            status=result.status,
        )

    async def undo(self) -> Response[Any]:
        payload = await self._transport.call(
            "undo",
        )

        return response.from_payload(
            payload,
        )

    async def redo(self) -> Response[Any]:
        payload = await self._transport.call(
            "redo",
        )

        return response.from_payload(
            payload,
        )

    async def to_dict(
        self,
        address: Address[T],
    ) -> Response[T]:
        payload = await self._transport.call(
            "to_dict",
            address.to_list(),
        )

        return response.from_payload(
            payload,
        )

    # -------------------------------------------------
    # Server events
    # -------------------------------------------------

    def on_event(
        self,
        message: dict[str, Any],
    ) -> None:
        event_token = message["event_token"]
        event_payload = message["event"]

        self.dispatch(
            event_token,
            event_payload,
        )

    def dispatch(
        self,
        event_token: str,
        payload: dict[str, Any],
    ) -> None:
        subscriptions = self._subscriptions.get(
            event_token,
        )

        if not subscriptions:
            return

        event: StoreEvent = event_from_payload(
            payload,
        )

        for subscription in tuple(subscriptions):
            subscription.dispatch(
                event,
            )
