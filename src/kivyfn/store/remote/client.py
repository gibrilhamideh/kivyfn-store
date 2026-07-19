# kivyfn/store/remote/client.py
from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import (
    Any,
    Literal,
    Protocol,
    TypeVar,
    overload,
)

from kivyfn.store.address import Address
from kivyfn.store.operations import OpCode
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
    event_from_payload,
)
from kivyfn.store.remote.subscription import (
    EventCallback,
    RemoteSubscription,
)
from kivyfn.store.transaction import TransactionResult
from kivyfn.store.transport import response
from kivyfn.store.transport.response import Response
from kivyfn.store.remote.transport import RemoteClientTransport


T = TypeVar("T")
RowT = TypeVar("RowT")


TransportHandler = Callable[
    [dict[str, Any]],
    Awaitable[None],
]





class RemoteSignalBindingProtocol(Protocol):
    """
    Minimal signal-binding interface required by RemoteStoreClient.

    The concrete RemoteSignalBinding will be implemented in remote/signal.py.
    """

    @property
    def token(self) -> str:
        ...

    @property
    def event_token(self) -> str:
        ...

    async def dispatch(
        self,
        event: Any,
    ) -> bool:
        ...

    def _mark_disposed(self) -> None:
        ...


class RemoteStoreClient:
    """
    Transport-independent remote-store client.

    Responsibilities:
    - connection lifecycle
    - request/response calls
    - remote subscription creation and tracking
    - remote signal-binding tracking
    - incoming event and signal dispatch
    - local cleanup when disconnected

    The client knows nothing about Socket.IO specifically.
    """

    def __init__(
        self,
        transport: RemoteClientTransport,
    ) -> None:
        self._transport = transport

        self._transport.set_event_handler(
            self.on_event,
        )
        self._transport.set_signal_handler(
            self.on_signal,
        )

        # event_token -> subscriptions sharing the remote event stream
        self._subscriptions: dict[
            str,
            set[RemoteSubscription[Any]],
        ] = defaultdict(set)

        # subscription token -> subscription
        self._subscriptions_by_token: dict[
            str,
            RemoteSubscription[Any],
        ] = {}

        # event_token -> signal bindings sharing the remote stream
        self._signal_bindings: dict[
            str,
            set[RemoteSignalBindingProtocol],
        ] = defaultdict(set)

        # binding token -> binding
        self._signal_bindings_by_token: dict[
            str,
            RemoteSignalBindingProtocol,
        ] = {}

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    async def connect(self) -> None:
        if self.connected:
            return

        await self._transport.connect()

    async def disconnect(self) -> None:
        if self.connected:
            await self._transport.disconnect()

        self._clear_local_state()

    @property
    def connected(self) -> bool:
        return self._transport.connected

    def _clear_local_state(self) -> None:
        subscriptions = tuple(
            self._subscriptions_by_token.values(),
        )

        signal_bindings = tuple(
            self._signal_bindings_by_token.values(),
        )

        self._subscriptions.clear()
        self._subscriptions_by_token.clear()

        self._signal_bindings.clear()
        self._signal_bindings_by_token.clear()

        for subscription in subscriptions:
            subscription._mark_disposed()

        for binding in signal_bindings:
            binding._mark_disposed()

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
        field_names = (
            [fields]
            if isinstance(fields, str)
            else list(fields)
        )

        payload = await self._transport.call(
            "lookup",
            {
                "address": address.to_list(),
                "fields": field_names,
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
            {
                "address": address.to_list(),
            },
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
        opcode: Literal[OpCode.SET],
        address: Address[Any],
        callback: EventCallback[SetEvent],
        *,
        id: None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[SetEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.SETROW],
        address: Address[Any],
        callback: EventCallback[SetRowEvent],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[SetRowEvent]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.INSERT],
        address: Address[Any],
        callback: EventCallback[InsertEvent[RowT]],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[InsertEvent[RowT]]]:
        ...

    @overload
    async def subscribe(
        self,
        opcode: Literal[OpCode.REMOVE],
        address: Address[Any],
        callback: EventCallback[RemoveEvent[RowT]],
        *,
        id: None = None,
        key: None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[RemoveEvent[RowT]]]:
        ...

    async def subscribe(
        self,
        opcode: OpCode,
        address: Address[Any],
        callback: EventCallback[Any],
        *,
        id: str | None = None,
        key: str | None = None,
        depth: int | None = 0,
        resolve_foreignkeys: bool = False,
        propagate_foreignkeys: bool = False,
    ) -> Response[RemoteSubscription[Any]]:
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

        if not result.ok:
            return result

        data = result.data

        if not isinstance(data, dict):
            raise RuntimeError(
                "Remote subscribe response must contain an object"
            )

        subscription_token = data.get(
            "subscription_token",
        )
        event_token = data.get(
            "event_token",
        )

        if not isinstance(subscription_token, str):
            raise RuntimeError(
                "Remote subscribe response is missing subscription_token"
            )

        if not isinstance(event_token, str):
            raise RuntimeError(
                "Remote subscribe response is missing event_token"
            )

        subscription: RemoteSubscription[Any] = RemoteSubscription(
            client=self,
            token=subscription_token,
            event_token=event_token,
            callback=callback,
        )

        self._subscriptions[
            event_token
        ].add(
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

        if result.ok:
            self._remove_subscription(
                token,
            )

        return result

    def _remove_subscription(
        self,
        token: str,
    ) -> RemoteSubscription[Any] | None:
        subscription = self._subscriptions_by_token.pop(
            token,
            None,
        )

        if subscription is None:
            return None

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

        subscription._mark_disposed()

        return subscription

    # -------------------------------------------------
    # Signal requests
    # -------------------------------------------------

    async def request_signal_bind(
        self,
        address: Address[Any],
        signal: str,
        *,
        id: str | None = None,
    ) -> Response[dict[str, Any]]:
        """
        Request a signal binding from the server.

        RemoteSignalManager creates the local binding object and registers it
        through register_signal_binding().
        """
        payload = await self._transport.call(
            "signal_bind",
            {
                "address": address.to_list(),
                "signal": signal,
                "id": id,
            },
        )

        return response.from_payload(
            payload,
        )

    async def request_signal_unbind(
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

        if result.ok:
            self.remove_signal_binding(
                token,
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

    def register_signal_binding(
        self,
        binding: RemoteSignalBindingProtocol,
    ) -> None:
        if binding.token in self._signal_bindings_by_token:
            raise RuntimeError(
                f"Signal binding token already registered: "
                f"{binding.token!r}"
            )

        self._signal_bindings[
            binding.event_token
        ].add(
            binding,
        )

        self._signal_bindings_by_token[
            binding.token
        ] = binding

    def remove_signal_binding(
        self,
        token: str,
    ) -> RemoteSignalBindingProtocol | None:
        binding = self._signal_bindings_by_token.pop(
            token,
            None,
        )

        if binding is None:
            return None

        bucket = self._signal_bindings.get(
            binding.event_token,
        )

        if bucket is not None:
            bucket.discard(
                binding,
            )

            if not bucket:
                self._signal_bindings.pop(
                    binding.event_token,
                    None,
                )

        binding._mark_disposed()

        return binding

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

        if not result.ok:
            return result

        if not isinstance(result.data, dict):
            raise RuntimeError(
                "Remote transaction response must contain an object"
            )

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
    # Incoming transport events
    # -------------------------------------------------

    async def on_event(
        self,
        message: dict[str, Any],
    ) -> None:
        event_token = message.get(
            "event_token",
        )
        event_payload = message.get(
            "event",
        )

        if not isinstance(event_token, str):
            raise ValueError(
                "Remote event message is missing event_token"
            )

        if not isinstance(event_payload, dict):
            raise ValueError(
                "Remote event message is missing event payload"
            )

        subscriptions = tuple(
            self._subscriptions.get(
                event_token,
                (),
            )
        )

        if not subscriptions:
            return

        event: StoreEvent = event_from_payload(
            event_payload,
        )

        dead_tokens: list[str] = []

        for subscription in subscriptions:
            if not subscription.dispatch(
                event,
            ):
                dead_tokens.append(
                    subscription.token,
                )

        for token in dead_tokens:
            await self.unsubscribe(
                token,
            )

    async def on_signal(
        self,
        message: dict[str, Any],
    ) -> None:
        # Imported lazily so remote/client.py does not create an import cycle
        # with remote/signal.py.
        from kivyfn.store.signals.event import signal_event_from_payload

        event_token = message.get(
            "event_token",
        )
        signal_payload = message.get(
            "signal",
        )

        if not isinstance(event_token, str):
            raise ValueError(
                "Remote signal message is missing event_token"
            )

        if not isinstance(signal_payload, dict):
            raise ValueError(
                "Remote signal message is missing signal payload"
            )

        bindings = tuple(
            self._signal_bindings.get(
                event_token,
                (),
            )
        )

        if not bindings:
            return

        event = signal_event_from_payload(
            signal_payload,
        )

        dead_tokens: list[str] = []

        for binding in bindings:
            if not await binding.dispatch(
                event,
            ):
                dead_tokens.append(
                    binding.token,
                )

        for token in dead_tokens:
            await self.request_signal_unbind(
                token,
            )
