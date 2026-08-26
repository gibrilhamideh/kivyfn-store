from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kivy.event import EventDispatcher
from kivy.properties import BooleanProperty

from kivyfn.store.error import StoreStateError
from kivyfn.store.state.change import StateChange


class NodeState(EventDispatcher):
    """
    Base observable state for one store model.

    Subclasses declare their fields with Kivy properties. State adapters
    hydrate and update those properties from store snapshots and events.

    ``exists`` belongs to the state layer and cannot be supplied by a store
    model. It allows a retained row reference to survive removal and later
    reinsertion.
    """

    exists: bool = BooleanProperty(False)

    __events__ = (
        "on_change",
    )

    def __init__(
        self,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

    # -------------------------------------------------
    # Internal state updates
    # -------------------------------------------------

    def _hydrate(
        self,
        values: Mapping[str, Any],
    ) -> None:
        """Apply a complete snapshot without emitting ``on_change``."""
        for key, value in values.items():
            self._set_property(
                key,
                value,
            )

        self.exists = True

    def _apply(
        self,
        key: str,
        value: Any,
        change: StateChange,
    ) -> None:
        """Apply one store event and emit its state-level change."""
        self._set_property(
            key,
            value,
        )

        self.dispatch(
            "on_change",
            change,
        )

    def _insert(
        self,
        values: Mapping[str, Any],
        change: StateChange,
    ) -> None:
        """Hydrate a retained reference after its row is inserted."""
        self._hydrate(
            values,
        )

        self.dispatch(
            "on_change",
            change,
        )

    def _remove(
        self,
        change: StateChange,
    ) -> None:
        """Mark a retained reference as absent and emit its removal."""
        self.exists = False

        self.dispatch(
            "on_change",
            change,
        )

    def _set_property(
        self,
        key: str,
        value: Any,
    ) -> None:
        if key == "exists":
            raise StoreStateError(
                "'exists' is reserved by NodeState"
            )

        if self.property(key, quiet=True) is None:
            raise StoreStateError(
                f"{type(self).__name__} has no state property {key!r}"
            )

        setattr(
            self,
            key,
            value,
        )

    # -------------------------------------------------
    # Events
    # -------------------------------------------------

    def on_change(
        self,
        change: StateChange,
    ) -> None:
        pass
