# kivyfn/store/server/signal.py
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from kivyfn.store.address import Address
from kivyfn.store.signals.event import SignalEvent


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine


@dataclass(frozen=True, slots=True)
class SignalListener:
    """
    Identifies one remotely observable store signal stream.

    A listener may target every row of a node when `id` is None, or one
    specific row when `id` is provided.
    """

    address: Address[Any]
    signal: str
    id: str | None

    @property
    def channel(self) -> str:
        """
        Return the transport-independent channel for this signal stream.

        Concrete transports may map this channel to a Socket.IO room, an
        internal Unix-socket connection group, or another routing mechanism.
        """
        address = ":".join(
            str(part)
            for part in self.address.to_list()
        )

        scope = self.id if self.id is not None else "*"

        return (
            f"store:signal:"
            f"{address}:"
            f"{self.signal}:"
            f"{scope}"
        )


@dataclass(slots=True)
class ServerSignalBinding:
    """
    Tracks one remote client's binding to a signal listener.
    """

    token: str
    client_id: str
    listener: SignalListener

    @property
    def event_token(self) -> str:
        """
        Return the stable token used to route incoming signal events.
        """
        return self.listener.channel


@dataclass(frozen=True, slots=True)
class SignalBindResult:
    """
    Result returned after registering a remote signal binding.

    `should_join_channel` is true only when this is the client's first binding
    to the corresponding channel.
    """

    binding_token: str
    event_token: str
    channel: str
    should_join_channel: bool


@dataclass(frozen=True, slots=True)
class SignalUnbindResult:
    """
    Result returned after removing a remote signal binding.

    `should_leave_channel` is true when the client no longer owns any binding
    associated with the corresponding channel.
    """

    channel: str
    should_leave_channel: bool


@dataclass(frozen=True, slots=True)
class SignalDispatchMessage:
    """
    Transport-independent outgoing signal event.

    The active server transport is responsible for delivering `payload` to
    every client associated with `channel`.
    """

    channel: str
    payload: dict[str, Any]


class RemoteSignalManager:
    """
    Server-side signal registry for remote store clients.

    This manager owns:

    - signal target validation
    - remote binding tokens
    - binding ownership by client
    - per-client listener reference counts
    - disconnect cleanup
    - transport-independent dispatch messages

    It accepts concrete, already parsed Python values. It does not parse raw
    transport payloads and does not perform Socket.IO, Unix-socket, TCP, or
    other transport operations directly.
    """

    def __init__(
        self,
        engine: Engine,
    ) -> None:
        self._engine = engine

        # binding token -> binding
        self._bindings: dict[
            str,
            ServerSignalBinding,
        ] = {}

        # listener -> binding tokens
        self._listener_bindings: dict[
            SignalListener,
            set[str],
        ] = defaultdict(set)

        # client id -> binding tokens
        self._client_bindings: dict[
            str,
            set[str],
        ] = defaultdict(set)

        # client id -> listener -> reference count
        self._client_listener_counts: dict[
            str,
            dict[SignalListener, int],
        ] = defaultdict(
            lambda: defaultdict(int)
        )

    # -------------------------------------------------
    # Bind / unbind
    # -------------------------------------------------

    def bind(
        self,
        client_id: str,
        address: Address[Any],
        signal: str,
        *,
        id: str | None = None,
    ) -> SignalBindResult:
        """
        Register one signal binding for a remote client.

        The address, signal name, and optional row ID must already have been
        parsed by the active transport. Store-level validation remains owned by
        this manager and the underlying engine.

        Returns routing information that allows the transport to join the
        client to the associated channel when necessary.
        """
        self._validate_client_id(
            client_id,
        )

        self._validate_signal_name(
            signal,
        )

        self._validate_row_id(
            id,
        )

        self._validate_target(
            address=address,
            signal=signal,
            id=id,
        )

        listener = SignalListener(
            address=address,
            signal=signal,
            id=id,
        )

        token = uuid4().hex

        binding = ServerSignalBinding(
            token=token,
            client_id=client_id,
            listener=listener,
        )

        should_join_channel = (
            self._client_listener_counts[
                client_id
            ][listener]
            == 0
        )

        self._bindings[
            token
        ] = binding

        self._listener_bindings[
            listener
        ].add(
            token,
        )

        self._client_bindings[
            client_id
        ].add(
            token,
        )

        self._client_listener_counts[
            client_id
        ][listener] += 1

        return SignalBindResult(
            binding_token=token,
            event_token=listener.channel,
            channel=listener.channel,
            should_join_channel=should_join_channel,
        )

    def unbind(
        self,
        client_id: str,
        token: str,
    ) -> SignalUnbindResult:
        """
        Remove one signal binding owned by a remote client.

        Raises KeyError when the binding does not exist or belongs to another
        client.

        Returns routing information that allows the transport to leave the
        associated channel when the client has no remaining bindings to it.
        """
        self._validate_client_id(
            client_id,
        )

        self._validate_token(
            token,
        )

        binding = self._bindings.get(
            token,
        )

        if (
            binding is None
            or binding.client_id != client_id
        ):
            raise KeyError(
                "Signal binding not found for this client"
            )

        listener = binding.listener

        self._remove_binding(
            binding,
        )

        listener_counts = (
            self._client_listener_counts.get(
                client_id,
            )
        )

        should_leave_channel = (
            listener_counts is None
            or listener not in listener_counts
        )

        return SignalUnbindResult(
            channel=listener.channel,
            should_leave_channel=should_leave_channel,
        )

    def remove_client(
        self,
        client_id: str,
    ) -> tuple[str, ...]:
        """
        Remove every signal binding owned by a disconnected client.

        The returned channels may be used by transports that require explicit
        channel cleanup. Socket.IO normally removes room membership itself when
        a client disconnects, while other transports may use the returned
        values directly.
        """
        self._validate_client_id(
            client_id,
        )

        channels: set[str] = set()

        tokens = tuple(
            self._client_bindings.get(
                client_id,
                (),
            )
        )

        for token in tokens:
            binding = self._bindings.get(
                token,
            )

            if binding is None:
                continue

            channels.add(
                binding.listener.channel,
            )

            self._remove_binding(
                binding,
            )

        self._client_bindings.pop(
            client_id,
            None,
        )

        self._client_listener_counts.pop(
            client_id,
            None,
        )

        return tuple(
            channels,
        )

    # -------------------------------------------------
    # Emit
    # -------------------------------------------------

    def emit(
        self,
        address: Address[Any],
        signal: str,
        data: Any = None,
        *,
        id: str | None = None,
    ) -> tuple[SignalDispatchMessage, ...]:
        """
        Create dispatch messages for one emitted store signal.

        A row-scoped signal is dispatched to both:

        - listeners bound to every row of the node
        - listeners bound to the specific row

        A node-scoped signal is dispatched only to listeners bound without a
        row ID.

        The transport is responsible for delivering the returned messages.
        """
        self._validate_signal_name(
            signal,
        )

        self._validate_row_id(
            id,
        )

        self._validate_target(
            address=address,
            signal=signal,
            id=id,
        )

        event = SignalEvent(
            address=address,
            signal=signal,
            id=id,
            data=data,
        )

        payload = event.payload()

        listeners = [
            SignalListener(
                address=address,
                signal=signal,
                id=None,
            ),
        ]

        if id is not None:
            listeners.append(
                SignalListener(
                    address=address,
                    signal=signal,
                    id=id,
                )
            )

        messages: list[
            SignalDispatchMessage
        ] = []

        for listener in listeners:
            if listener not in self._listener_bindings:
                continue

            messages.append(
                SignalDispatchMessage(
                    channel=listener.channel,
                    payload={
                        "event_token": listener.channel,
                        "signal": payload,
                    },
                )
            )

        return tuple(
            messages,
        )

    # -------------------------------------------------
    # Binding lifecycle
    # -------------------------------------------------

    def _remove_binding(
        self,
        binding: ServerSignalBinding,
    ) -> None:
        """
        Remove one binding from every internal registry.
        """
        token = binding.token
        client_id = binding.client_id
        listener = binding.listener

        self._bindings.pop(
            token,
            None,
        )

        listener_bindings = (
            self._listener_bindings.get(
                listener,
            )
        )

        if listener_bindings is not None:
            listener_bindings.discard(
                token,
            )

            if not listener_bindings:
                self._listener_bindings.pop(
                    listener,
                    None,
                )

        client_bindings = (
            self._client_bindings.get(
                client_id,
            )
        )

        if client_bindings is not None:
            client_bindings.discard(
                token,
            )

            if not client_bindings:
                self._client_bindings.pop(
                    client_id,
                    None,
                )

        listener_counts = (
            self._client_listener_counts.get(
                client_id,
            )
        )

        if listener_counts is None:
            return

        count = listener_counts.get(
            listener,
            0,
        )

        if count <= 1:
            listener_counts.pop(
                listener,
                None,
            )
        else:
            listener_counts[
                listener
            ] = count - 1

        if not listener_counts:
            self._client_listener_counts.pop(
                client_id,
                None,
            )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    def _validate_target(
        self,
        *,
        address: Address[Any],
        signal: str,
        id: str | None,
    ) -> None:
        """
        Validate that the requested node, signal, and optional row exist.
        """
        node = self._engine.get_node_by_address(
            address,
        )

        node.schema.get_signal(
            signal,
        )

        if id is not None:
            node.snapshot(
                id=id,
            )

    @staticmethod
    def _validate_client_id(
        client_id: str,
    ) -> None:
        """
        Validate the transport-provided client identifier.
        """
        if not isinstance(
            client_id,
            str,
        ) or not client_id:
            raise TypeError(
                "Signal client ID must be a non-empty string"
            )

    @staticmethod
    def _validate_signal_name(
        signal: str,
    ) -> None:
        """
        Validate the concrete signal name.
        """
        if not isinstance(
            signal,
            str,
        ) or not signal:
            raise TypeError(
                "Signal name must be a non-empty string"
            )

    @staticmethod
    def _validate_row_id(
        id: str | None,
    ) -> None:
        """
        Validate the optional signal row identifier.
        """
        if (
            id is not None
            and not isinstance(id, str)
        ):
            raise TypeError(
                "Signal row ID must be a string or None"
            )

    @staticmethod
    def _validate_token(
        token: str,
    ) -> None:
        """
        Validate a remote signal binding token.
        """
        if not isinstance(
            token,
            str,
        ) or not token:
            raise TypeError(
                "Signal binding token must be a non-empty string"
            )
