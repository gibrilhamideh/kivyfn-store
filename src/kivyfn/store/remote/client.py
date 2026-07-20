# kivyfn/store/remote/client.py
from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import (
    Any,
    Literal,
    Protocol,
    TypeVar,
    cast,
    overload,
)

from kivyfn.store.address import Address
from kivyfn.store.error import (
    StoreProtocolError,
    StoreStateError,
)
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
    event_from_payload,
)
from kivyfn.store.operations import OpCode
from kivyfn.store.remote.subscription import (
    EventCallback,
    RemoteSubscription,
)
from kivyfn.store.remote.transport import RemoteClientTransport
from kivyfn.store.response import (
    Response,
    from_payload,
    success,
)
from kivyfn.store.transaction import TransactionResult


T = TypeVar("T")
RowT = TypeVar("RowT")


TransportHandler = Callable[
    [dict[str, Any]],
    Awaitable[None],
]


class RemoteSignalBindingProtocol(Protocol):
    """
    Minimal signal-binding interface required by RemoteStoreClient.

    The concrete RemoteSignalBinding is implemented in remote/signal.py.
    """

    @property
    def token(
        self,
    ) -> str:
        ...

    @property
    def event_token(
        self,
    ) -> str:
        ...

    async def dispatch(
        self,
        event: Any,
    ) -> bool:
        ...

    def _mark_disposed(
        self,
    ) -> None:
        ...


class RemoteStoreClient:
    """
    Transport-independent remote-store client.

    Responsibilities:

    - connection lifecycle;
    - request and response calls;
    - response-envelope validation;
    - remote subscription creation and tracking;
    - remote signal-binding tracking;
    - incoming event and signal dispatch;
    - local cleanup when disconnected.

    Expected store failures are returned as FailureResponse values.
    Connection, timeout, state, and protocol failures remain exceptions.

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

    async def connect(
        self,
    ) -> None:
        if self.connected:
            return

        await self._transport.connect()

    async def disconnect(
        self,
    ) -> None:
        if self.connected:
            await self._transport.disconnect()

        self._clear_local_state()

    @property
    def connected(
        self,
    ) -> bool:
        return self._transport.connected

    def _require_connected(
        self,
    ) -> None:
        if not self.connected:
            raise StoreStateError(
                "Remote store client is not connected"
            )

    def _clear_local_state(
        self,
    ) -> None:
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
    # Transport request helper
    # -------------------------------------------------

    async def _request(
        self,
        event: str,
        message: Any | None = None,
    ) -> Response[Any]:
        self._require_connected()

        payload = await self._transport.call(
            event,
            message,
        )

        return from_payload(
            payload,
        )

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
        return await self._request(
            "snapshot",
            {
                "address": address.to_list(),
                "id": id,
                "key": key,
                "resolve_foreignkeys": resolve_foreignkeys,
            },
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

        result = await self._request(
            "lookup",
            {
                "address": address.to_list(),
                "fields": field_names,
                "value": value,
                "first": first,
            },
        )

        return cast(
            Response[T | dict[str, T] | None],
            result,
        )

    async def to_dict(
        self,
        address: Address[T],
    ) -> Response[T]:
        result = await self._request(
            "to_dict",
            {
                "address": address.to_list(),
            },
        )

        return cast(
            Response[T],
            result,
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
        result = await self._request(
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

        if result.ok is False:
            return cast(
                Response[RemoteSubscription[Any]],
                result,
            )

        data = result.data

        if not isinstance(
            data,
            dict,
        ):
            raise StoreProtocolError(
                "Remote subscribe response data must be an object"
            )

        subscription_token = data.get(
            "subscription_token",
        )

        event_token = data.get(
            "event_token",
        )

        if (
            not isinstance(subscription_token, str)
            or not subscription_token
        ):
            raise StoreProtocolError(
                "Remote subscribe response is missing a valid "
                "subscription_token"
            )

        if (
            not isinstance(event_token, str)
            or not event_token
        ):
            raise StoreProtocolError(
                "Remote subscribe response is missing a valid event_token"
            )

        if subscription_token in self._subscriptions_by_token:
            raise StoreProtocolError(
                "Remote server returned a duplicate subscription token: "
                f"{subscription_token!r}"
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

        return success(
            subscription,
        )

    async def unsubscribe(
        self,
        token: str,
    ) -> Response[bool]:
        result = await self._request(
            "unsubscribe",
            {
                "subscription_token": token,
            },
        )

        if result.ok:
            self._remove_subscription(
                token,
            )

        return cast(
            Response[bool],
            result,
        )

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
        result = await self._request(
            "signal_bind",
            {
                "address": address.to_list(),
                "signal": signal,
                "id": id,
            },
        )

        return cast(
            Response[dict[str, Any]],
            result,
        )

    async def request_signal_unbind(
        self,
        token: str,
    ) -> Response[bool]:
        result = await self._request(
            "signal_unbind",
            {
                "binding_token": token,
            },
        )

        if result.ok:
            self.remove_signal_binding(
                token,
            )

        return cast(
            Response[bool],
            result,
        )

    async def emit_signal(
        self,
        address: Address[Any],
        signal: str,
        data: Any = None,
        *,
        id: str | None = None,
    ) -> Response[bool]:
        result = await self._request(
            "signal_emit",
            {
                "address": address.to_list(),
                "signal": signal,
                "id": id,
                "data": data,
            },
        )

        return cast(
            Response[bool],
            result,
        )

    def register_signal_binding(
        self,
        binding: RemoteSignalBindingProtocol,
    ) -> None:
        if binding.token in self._signal_bindings_by_token:
            raise StoreStateError(
                "Signal binding token is already registered: "
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
        result = await self._request(
            "transaction",
            {
                "instructions": instructions,
            },
        )

        if result.ok is False:
            return cast(
                Response[TransactionResult],
                result,
            )

        data = result.data

        if not isinstance(
            data,
            dict,
        ):
            raise StoreProtocolError(
                "Remote transaction response data must be an object"
            )

        try:
            transaction_result = TransactionResult.from_payload(
                data,
            )

        except Exception as exc:
            raise StoreProtocolError(
                "Remote transaction response contains an invalid "
                "transaction result"
            ) from exc

        return success(
            transaction_result,
        )

    async def undo(
        self,
    ) -> Response[Any]:
        return await self._request(
            "undo",
        )

    async def redo(
        self,
    ) -> Response[Any]:
        return await self._request(
            "redo",
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

        if (
            not isinstance(event_token, str)
            or not event_token
        ):
            raise StoreProtocolError(
                "Remote event message is missing a valid event_token"
            )

        if not isinstance(
            event_payload,
            dict,
        ):
            raise StoreProtocolError(
                "Remote event message is missing an event payload"
            )

        subscriptions = tuple(
            self._subscriptions.get(
                event_token,
                (),
            )
        )

        if not subscriptions:
            return

        try:
            event: StoreEvent = event_from_payload(
                event_payload,
            )

        except Exception as exc:
            raise StoreProtocolError(
                "Remote event payload is invalid"
            ) from exc

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
        from kivyfn.store.signals.event import (
            signal_event_from_payload,
        )

        event_token = message.get(
            "event_token",
        )

        signal_payload = message.get(
            "signal",
        )

        if (
            not isinstance(event_token, str)
            or not event_token
        ):
            raise StoreProtocolError(
                "Remote signal message is missing a valid event_token"
            )

        if not isinstance(
            signal_payload,
            dict,
        ):
            raise StoreProtocolError(
                "Remote signal message is missing a signal payload"
            )

        bindings = tuple(
            self._signal_bindings.get(
                event_token,
                (),
            )
        )

        if not bindings:
            return

        try:
            event = signal_event_from_payload(
                signal_payload,
            )

        except Exception as exc:
            raise StoreProtocolError(
                "Remote signal payload is invalid"
            ) from exc

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
