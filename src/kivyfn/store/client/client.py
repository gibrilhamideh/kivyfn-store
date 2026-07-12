# kivyfn/store/client/client.py
from __future__ import annotations

from collections import defaultdict
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Literal,
    TypeAlias,
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
from kivyfn.store.signals.binding import SignalBinding
from kivyfn.store.signals.event import (
    SignalEvent,
    signal_event_from_payload,
)
from kivyfn.store.transaction import TransactionResult
from kivyfn.store.transport import SocketIOClient, response
from kivyfn.store.transport.response import Response


if TYPE_CHECKING:
    from kivyfn.store import Store


T = TypeVar("T")

SignalCallback: TypeAlias = Callable[
    [SignalEvent[T]],
    None | Awaitable[None],
]


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
            event_handler=self.on_event,
            signal_handler=self.on_signal,
        )

        # -------------------------------------------------
        # Subscriptions
        # -------------------------------------------------

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

        # -------------------------------------------------
        # Signal bindings
        # -------------------------------------------------

        # event_token -> local bindings using this signal stream
        self._signal_bindings: dict[
            str,
            set[SignalBinding[Any]],
        ] = defaultdict(set)

        # binding_token -> local signal binding
        self._signal_bindings_by_token: dict[
            str,
            SignalBinding[Any],
        ] = {}

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def connect(self) -> None:
        await self._transport.connect()

    async def disconnect(self) -> None:
        await self._transport.disconnect()

        self._subscriptions.clear()
        self._subscriptions_by_token.clear()

        self._signal_bindings.clear()
        self._signal_bindings_by_token.clear()

    # -------------------------------------------------
    # Reads
    # -------------------------------------------------

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

        return response.from_payload(
            payload,
        )

    async def lookup(
        self,
        address: Address[T],
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> Response[T | dict[str, T] | None]:
        fields_payload = (
            [fields]
            if isinstance(fields, str)
            else list(fields)
        )

        payload = await self._transport.call(
            "lookup",
            {
                "address": address.to_list(),
                "fields": fields_payload,
                "value": value,
                "first": first,
            },
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

        result = response.from_payload(
            payload,
        )

        if result.ok is False:
            return result

        subscription_token = result.data[
            "subscription_token"
        ]
        event_token = result.data[
            "event_token"
        ]

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

        result = response.from_payload(
            payload,
        )

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
    # Signals
    # -------------------------------------------------

    async def bind_signal(
        self,
        address: Address[Any],
        signal: str,
        callback: SignalCallback[T],
        *,
        id: str | None = None,
    ) -> Response[SignalBinding[T]]:
        payload = await self._transport.call(
            "signal_bind",
            {
                "address": address.to_list(),
                "signal": signal,
                "id": id,
            },
        )

        result = response.from_payload(
            payload,
        )

        if result.ok is False:
            return result

        binding_token = result.data[
            "binding_token"
        ]
        event_token = result.data[
            "event_token"
        ]

        binding: SignalBinding[T] = SignalBinding(
            store=self.store,
            client=self,
            token=binding_token,
            event_token=event_token,
            callback=callback,
        )

        self._signal_bindings[event_token].add(
            binding,
        )

        self._signal_bindings_by_token[
            binding_token
        ] = binding

        return response.success(
            data=binding,
            status=result.status,
        )

    async def unbind_signal(
        self,
        token: str,
    ) -> Response[bool]:
        payload = await self._transport.call(
            "signal_unbind",
            {
                "token": token,
            },
        )

        result = response.from_payload(
            payload,
        )

        if result.ok is False:
            return result

        binding = self._signal_bindings_by_token.pop(
            token,
            None,
        )

        if binding is not None:
            event_token = binding.event_token

            bucket = self._signal_bindings.get(
                event_token,
            )

            if bucket is not None:
                bucket.discard(
                    binding,
                )

                if not bucket:
                    self._signal_bindings.pop(
                        event_token,
                        None,
                    )

        return result

    async def emit_signal(
        self,
        address: Address[Any],
        signal: str,
        data: Any = None,
        *,
        id: str | None = None,
    ) -> Response[bool]:
        payload = await self._transport.call(
            "signal_emit",
            {
                "address": address.to_list(),
                "signal": signal,
                "id": id,
                "data": data,
            },
        )

        return response.from_payload(
            payload,
        )

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

        result = response.from_payload(
            payload,
        )

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

    # -------------------------------------------------
    # Server events
    # -------------------------------------------------

    async def on_event(
        self,
        message: dict[str, Any],
    ) -> None:
        event_token = message["event_token"]
        event_payload = message["event"]

        self.dispatch(
            event_token,
            event_payload,
        )

    async def on_signal(
        self,
        message: dict[str, Any],
    ) -> None:
        event_token = message["event_token"]
        signal_payload = message["signal"]

        await self.dispatch_signal(
            event_token,
            signal_payload,
        )

    # -------------------------------------------------
    # Local dispatch
    # -------------------------------------------------

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

    async def dispatch_signal(
        self,
        event_token: str,
        payload: dict[str, Any],
    ) -> None:
        bindings = self._signal_bindings.get(
            event_token,
        )

        if not bindings:
            return

        event: SignalEvent[Any] = signal_event_from_payload(
            payload,
        )

        for binding in tuple(bindings):
            await binding.dispatch(
                event,
            )
