# kivyfn/store/signals/manager.py
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.signals.binding import EngineSignalBinding
from kivyfn.store.signals.event import SignalEvent
from kivyfn.store.signals.listener import (
    SignalListener,
    SignalListenerIndex,
)


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


SignalIndexKey = tuple[Address[Any], str]


@dataclass(slots=True, frozen=True)
class BindSignalResult:
    binding_token: str
    event_token: str
    room: str
    should_join_room: bool


@dataclass(slots=True, frozen=True)
class UnbindSignalResult:
    room: str
    should_leave_room: bool


@dataclass(slots=True, frozen=True)
class SignalDispatchMessage:
    room: str
    payload: dict[str, Any]


class SignalManager:
    def __init__(
        self,
        engine: Engine,
    ) -> None:
        self._engine = engine

        # -------------------------------------------------
        # Listener indexing
        # -------------------------------------------------

        self._indexes: dict[
            SignalIndexKey,
            SignalListenerIndex,
        ] = defaultdict(SignalListenerIndex)

        # -------------------------------------------------
        # Binding tracking
        # -------------------------------------------------

        self._bindings: dict[
            str,
            EngineSignalBinding,
        ] = {}

        self._listener_bindings: dict[
            SignalListener,
            set[EngineSignalBinding],
        ] = defaultdict(set)

        # -------------------------------------------------
        # Per-client tracking
        # -------------------------------------------------

        self._sid_listener_counts: dict[
            str,
            dict[SignalListener, int],
        ] = defaultdict(lambda: defaultdict(int))

        self._sid_bindings: dict[
            str,
            set[str],
        ] = defaultdict(set)

    # -------------------------------------------------
    # Bind / unbind
    # -------------------------------------------------

    def bind(
        self,
        sid: str,
        message: dict[str, Any],
    ) -> BindSignalResult:
        address = Address(
            *message["address"],
        )
        signal_name = message["signal"]
        row_id = message.get("id")

        node = self._engine.get_node_by_address(
            address,
        )

        node.schema.get_signal(
            signal_name,
        )

        self._validate_scope(
            node=node,
            signal_name=signal_name,
            row_id=row_id,
        )

        listener = SignalListener(
            address=address,
            signal=signal_name,
            id=row_id,
        )

        index_key = (
            address,
            signal_name,
        )

        self._indexes[
            index_key
        ].add(listener)

        binding = EngineSignalBinding(
            node=node,
            listener=listener,
        )

        self._bindings[
            binding.token
        ] = binding

        self._listener_bindings[
            listener
        ].add(binding)

        should_join_room = (
            self._sid_listener_counts[sid][listener] == 0
        )

        self._sid_listener_counts[
            sid
        ][listener] += 1

        self._sid_bindings[
            sid
        ].add(binding.token)

        return BindSignalResult(
            binding_token=binding.token,
            event_token=listener.room,
            room=listener.room,
            should_join_room=should_join_room,
        )

    def unbind(
        self,
        sid: str,
        token: str,
    ) -> UnbindSignalResult:
        sid_bindings = self._sid_bindings.get(
            sid,
        )

        if sid_bindings is None or token not in sid_bindings:
            raise KeyError(
                "Signal binding not found for this client"
            )

        sid_bindings.discard(
            token,
        )

        if not sid_bindings:
            self._sid_bindings.pop(
                sid,
                None,
            )

        binding = self._bindings.pop(
            token,
        )

        listener = binding.listener

        self._remove_binding_from_listener(
            binding,
        )

        listener_counts = self._sid_listener_counts.get(
            sid,
        )

        if listener_counts is None:
            raise RuntimeError(
                "Signal listener count missing for client"
            )

        listener_counts[listener] -= 1

        should_leave_room = (
            listener_counts[listener] == 0
        )

        if should_leave_room:
            listener_counts.pop(
                listener,
                None,
            )

        if not listener_counts:
            self._sid_listener_counts.pop(
                sid,
                None,
            )

        return UnbindSignalResult(
            room=listener.room,
            should_leave_room=should_leave_room,
        )

    def remove_sid(
        self,
        sid: str,
    ) -> list[str]:
        rooms: list[str] = []

        tokens = tuple(
            self._sid_bindings.pop(
                sid,
                (),
            )
        )

        for token in tokens:
            binding = self._bindings.pop(
                token,
                None,
            )

            if binding is None:
                continue

            rooms.append(
                binding.room,
            )

            self._remove_binding_from_listener(
                binding,
            )

        self._sid_listener_counts.pop(
            sid,
            None,
        )

        return rooms

    # -------------------------------------------------
    # Emit
    # -------------------------------------------------

    def emit(
        self,
        message: dict[str, Any],
    ) -> list[SignalDispatchMessage]:
        address = Address(
            *message["address"],
        )
        signal_name = message["signal"]
        row_id = message.get("id")
        data = message.get("data")

        node = self._engine.get_node_by_address(
            address,
        )

        node.schema.get_signal(
            signal_name,
        )

        self._validate_scope(
            node=node,
            signal_name=signal_name,
            row_id=row_id,
        )

        index = self._indexes.get(
            (
                address,
                signal_name,
            )
        )

        if index is None:
            return []

        event = SignalEvent(
            address=address,
            signal=signal_name,
            id=row_id,
            data=data,
        )

        messages: list[SignalDispatchMessage] = []

        for listener in index.listeners(
            row_id,
        ):
            if listener not in self._listener_bindings:
                continue

            messages.append(
                SignalDispatchMessage(
                    room=listener.room,
                    payload={
                        "event_token": listener.room,
                        "signal": event.payload(),
                    },
                )
            )

        return messages

    # -------------------------------------------------
    # Binding lifecycle
    # -------------------------------------------------

    def _remove_binding_from_listener(
        self,
        binding: EngineSignalBinding,
    ) -> None:
        listener = binding.listener

        bindings = self._listener_bindings.get(
            listener,
        )

        if bindings is None:
            return

        bindings.discard(
            binding,
        )

        if bindings:
            return

        self._listener_bindings.pop(
            listener,
            None,
        )

        index_key = (
            listener.address,
            listener.signal,
        )

        index = self._indexes.get(
            index_key,
        )

        if index is None:
            return

        index.remove(
            listener,
        )

        if not index:
            self._indexes.pop(
                index_key,
                None,
            )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    def _validate_scope(
        self,
        *,
        node: Node,
        signal_name: str,
        row_id: str | None,
    ) -> None:
        if not node.schema.many:
            if row_id is not None:
                raise ValueError(
                    f"Signal {signal_name!r} on "
                    f"{node.schema.name} does not accept an id"
                )

            return

        if row_id is None:
            return

        if not node.exists(
            row_id,
        ):
            raise ValueError(
                f"Row {row_id!r} does not exist in "
                f"{node.schema.name}"
            )
