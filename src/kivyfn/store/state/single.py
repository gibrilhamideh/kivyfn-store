from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from kivy.event import EventDispatcher
from kivy.properties import ObjectProperty

from kivyfn.store.adapter.single import Adapter
from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import SetEvent
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.response import Response
from kivyfn.store.state.change import StateChange
from kivyfn.store.state.node import NodeState
from kivyfn.store.transaction import TransactionResult


ModelT = TypeVar(
    "ModelT",
    bound=Mapping[str, Any],
)

StateT = TypeVar(
    "StateT",
    bound=NodeState,
)


class SingleNodeState(
    EventDispatcher,
    Generic[ModelT, StateT],
):
    """
    Project one many=False store schema into observable Kivy state.

    The existing scalar adapter remains responsible for snapshots,
    subscriptions, commands, and store-level failures. This class translates
    its retained mapping and events into a stable ``NodeState`` instance.
    """

    state: StateT = ObjectProperty(
        None,
        rebind=True,
    )

    __events__ = (
        "on_loaded",
        "on_change",
        "on_set_failed",
        "on_sync_lost",
        "on_resynchronized",
    )

    def __init__(
        self,
        store: RemoteStore,
        address: Address[ModelT],
        state_type: type[StateT],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self._adapter = Adapter[ModelT](
            store=store,
            address=address,
        )

        state = state_type()

        if not isinstance(state, NodeState):
            raise StoreStateError(
                f"{state_type.__name__} must inherit from NodeState"
            )

        self.state = state

        self._adapter.bind(
            on_loaded=self._on_adapter_loaded,
            on_set=self._on_adapter_set,
            on_set_failed=self._on_adapter_set_failed,
            on_sync_lost=self._on_adapter_sync_lost,
            on_resynchronized=(
                self._on_adapter_resynchronized
            ),
        )

    # -------------------------------------------------
    # Loading
    # -------------------------------------------------

    async def load(self) -> None:
        await self._adapter.load()

    # -------------------------------------------------
    # Commands
    # -------------------------------------------------

    async def set(
        self,
        key: str,
        value: Any,
    ) -> Response[TransactionResult]:
        return await self._adapter.set(
            key=key,
            value=value,
        )

    # -------------------------------------------------
    # Adapter events
    # -------------------------------------------------

    def _on_adapter_loaded(
        self,
        adapter: Adapter[ModelT],
    ) -> None:
        self.state._hydrate(
            adapter.data,
        )

        self.dispatch(
            "on_loaded",
        )

    def _on_adapter_set(
        self,
        _adapter: Adapter[ModelT],
        event: SetEvent,
    ) -> None:
        change = StateChange(
            kind="set",
            path=(event.key,),
            old_value=event.old_value,
            new_value=event.new_value,
        )

        self.state._apply(
            key=event.key,
            value=event.new_value,
            change=change,
        )

        self.dispatch(
            "on_change",
            change,
        )

    def _on_adapter_set_failed(
        self,
        _adapter: Adapter[ModelT],
        key: str,
        previous_value: Any,
        error: Any,
    ) -> None:
        self.dispatch(
            "on_set_failed",
            key,
            previous_value,
            error,
        )

    def _on_adapter_sync_lost(
        self,
        _adapter: Adapter[ModelT],
        reason: str,
    ) -> None:
        self.dispatch(
            "on_sync_lost",
            reason,
        )

    def _on_adapter_resynchronized(
        self,
        adapter: Adapter[ModelT],
        reason: str,
    ) -> None:
        self.state._hydrate(
            adapter.data,
        )
        self.dispatch(
            "on_resynchronized",
            reason,
        )

    # -------------------------------------------------
    # State
    # -------------------------------------------------

    @property
    def store(self) -> RemoteStore:
        return self._adapter.store

    @property
    def address(self) -> Address[ModelT]:
        return self._adapter.address

    # -------------------------------------------------
    # Events
    # -------------------------------------------------

    def on_loaded(self) -> None:
        pass

    def on_change(
        self,
        change: StateChange,
    ) -> None:
        pass

    def on_set_failed(
        self,
        key: str,
        previous_value: Any,
        error: Any,
    ) -> None:
        pass

    def on_sync_lost(
        self,
        reason: str,
    ) -> None:
        pass

    def on_resynchronized(
        self,
        reason: str,
    ) -> None:
        pass
