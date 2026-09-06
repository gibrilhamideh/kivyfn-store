from __future__ import annotations

from typing import Any

from kivy.event import EventDispatcher
from kivy.properties import BooleanProperty

from kivyfn.store.state.operation import StateOperation, StateResult


class StateEvents(EventDispatcher):
    """Shared optional status and operation events for live state."""

    synced = BooleanProperty(False)
    pending = BooleanProperty(False)

    __events__ = (
        "on_success",
        "on_failed",
    )

    def __init__(self, **kwargs: Any) -> None:
        self._pending_operation_ids: set[str] = set()
        super().__init__(**kwargs)

    def _begin_operation(self, operation: StateOperation) -> None:
        self._pending_operation_ids.add(operation.id)
        self.pending = True

    def _finish_operation(
        self,
        operation: StateOperation,
        result: StateResult,
    ) -> None:
        self._pending_operation_ids.discard(operation.id)
        self.pending = bool(self._pending_operation_ids)

        if result.ok:
            self.dispatch("on_success", operation)
        else:
            self.dispatch("on_failed", operation)

    def on_success(self, operation: StateOperation) -> None:
        pass

    def on_failed(self, operation: StateOperation) -> None:
        pass
