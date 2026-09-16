# src/kivyfn/store/state/transaction/transaction.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, Self

from kivy.event import EventDispatcher
from kivyfn.logging import get_logger

from kivyfn.store.state.reactive.data import StateData
from kivyfn.store.state.transaction.action import (
    InsertAction,
    RemovalOutcome,
    RemoveAction,
    SetAction,
    SetRowAction,
    SignalAction,
    TransactionAction,
)
from kivyfn.store.state.transaction.failure import TransactionFailure
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store.state.reactive.collection import CollectionState
    from kivyfn.store.state.reactive.state import State


logger = get_logger(__name__)


class TransactionManager(Protocol):
    def enter_transaction(self, transaction: StateTransaction) -> object: ...

    def leave_transaction(self, token: object) -> None: ...

    def request_transaction(self, transaction: StateTransaction) -> None: ...

    def abort_transaction(
        self, transaction: StateTransaction, error: BaseException
    ) -> None: ...


class StateTransaction(EventDispatcher):
    """One atomic State transaction and its lifecycle."""

    __events__ = ("on_success", "on_failed", "on_done")

    def __init__(
        self,
        manager: TransactionManager,
        *,
        history_group: str | None = None,
        history_label: str | None = None,
    ) -> None:
        super().__init__()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "StateTransaction requires a running asyncio loop"
            ) from exc

        self.id = generate_uuid()
        self._history_group = history_group
        self._history_label = history_label

        self._owner_task = asyncio.current_task()
        self._manager = manager
        self._loop = loop
        self._token: object | None = None
        self._entered = False
        self._closed = False
        self._done = False
        self._failure: TransactionFailure | None = None
        self._completion = loop.create_future()

        self._actions: list[TransactionAction] = []

        self._setrows: dict[State[Any], SetRowAction] = {}
        self._inserts: dict[State[Any], InsertAction] = {}
        self._removes: dict[State[Any], RemoveAction] = {}
        self._signal_states: set[State[Any]] = set()

    def __enter__(self) -> StateTransaction:
        if self._entered:
            raise RuntimeError("State transaction cannot be entered twice")

        if self._closed:
            raise RuntimeError("State transaction is already closed")

        self._token = self._manager.enter_transaction(self)
        self._entered = True

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        del exc_type
        del traceback

        token = self._token

        if token is None:
            raise RuntimeError("State transaction has no active context token")

        self._manager.leave_transaction(token)
        self._token = None
        self._entered = False

        if exc is not None:
            self._closed = True
            self._manager.abort_transaction(self, exc)
            return False

        self._submit()

        return False

    def __await__(self):
        return self.wait().__await__()

    async def wait(self) -> Self:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "StateTransaction must be awaited on its owning event loop"
            )

        if self._entered:
            raise RuntimeError(
                "StateTransaction cannot be awaited while its context is open"
            )

        if not self._closed:
            raise RuntimeError(
                "StateTransaction cannot be awaited before submission"
            )

        await asyncio.shield(self._completion)

        return self

    @property
    def history_group(self) -> str | None:
        return self._history_group

    @property
    def history_label(self) -> str | None:
        return self._history_label

    @property
    def owner_task(self) -> asyncio.Task[Any] | None:
        return self._owner_task

    @property
    def actions(self) -> tuple[TransactionAction, ...]:
        return tuple(self._actions)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def done(self) -> bool:
        return self._done

    @property
    def success(self) -> bool:
        return self._done and self._failure is None

    @property
    def failed(self) -> bool:
        return self._done and self._failure is not None

    @property
    def failure(self) -> TransactionFailure | None:
        return self._failure

    def add_set(self, state: State[Any], values: dict[str, Any]) -> None:
        """Merge adjacent sets while preserving intervening operations."""
        self._require_open()

        if state.row_id is not None:
            raise ValueError("SetAction requires a State without a row ID")

        copied = StateData.copy_values(values)
        previous = self._actions[-1] if self._actions else None
        if isinstance(previous, SetAction) and previous.state is state:
            previous.values.update(copied)
            return

        self._actions.append(SetAction(state=state, values=copied))

    def add_setrow(
        self,
        state: State[Any],
        values: dict[str, Any],
        writes: dict[tuple[str, str, str], Any] | None = None,
    ) -> None:
        self._require_open()

        if state.row_id is None:
            raise ValueError("SetRowAction requires a State row ID")

        copied = StateData.copy_values(values)
        inserted = self._inserts.get(state)

        if inserted is not None and self._actions[-1] is inserted:
            inserted.values.update(copied)
            return

        if state in self._removes and inserted is None:
            raise RuntimeError(
                "A removed State row cannot be updated in one transaction"
            )

        action = self._setrows.get(state)

        # Only adjacent writes can coalesce: another projection may have
        # written the same canonical field between two calls on this State.
        if action is None or self._actions[-1] is not action:
            action = SetRowAction(
                state=state, values=copied, writes=writes or {}
            )
            self._setrows[state] = action
            self._actions.append(action)
            return

        action.values.update(copied)
        if writes is not None:
            action.writes.update(StateData.copy(writes))

    def add_insert(
        self,
        collection: CollectionState[Any],
        state: State[Any],
        values: dict[str, Any],
    ) -> None:
        self._require_open()

        if state.row_id is None:
            raise ValueError("InsertAction requires a State row ID")

        if state in self._inserts:
            raise RuntimeError(
                "A State row cannot be inserted twice in one transaction"
            )

        if state in self._setrows and state not in self._removes:
            raise RuntimeError(
                "An existing State row must be removed before insertion"
            )

        action = InsertAction(
            collection=collection,
            state=state,
            values=StateData.copy_values(values),
        )

        self._inserts[state] = action
        self._actions.append(action)

    def add_remove(
        self, collection: CollectionState[Any], state: State[Any]
    ) -> RemovalOutcome:
        """Record a removal or cancel a trailing insert without reordering.

        Cancelling a replacement preserves the row's earlier removal. An
        intervening action prevents cancellation because it may observe the
        inserted row. The manager uses this outcome to update overlays.
        """
        self._require_open()

        if state.row_id is None:
            raise ValueError("RemoveAction requires a State row ID")

        if state in self._signal_states:
            raise RuntimeError(
                "A signalled State row cannot be removed "
                "in the same transaction"
            )

        inserted = self._inserts.pop(state, None)

        if inserted is not None and self._actions[-1] is inserted:
            self._actions.pop()
            if state in self._removes:
                return RemovalOutcome.CANCELLED_REPLACEMENT
            return RemovalOutcome.CANCELLED_INSERT

        if state in self._removes and inserted is None:
            raise RuntimeError(
                "A State row cannot be removed twice in one transaction"
            )

        # Earlier actions may have been observed by intervening triggers.
        # Removing the row cannot erase those instructions.
        self._setrows.pop(state, None)
        action = RemoveAction(collection=collection, state=state)

        self._removes[state] = action
        self._actions.append(action)

        return RemovalOutcome.RECORDED

    def add_signal(self, state: State[Any], signal: str, data: Any) -> None:
        self._require_open()

        if not signal:
            raise ValueError("State signal name must be non-empty")

        if state in self._removes and state not in self._inserts:
            raise RuntimeError(
                "A removed State row cannot emit a signal "
                "in the same transaction"
            )

        self._signal_states.add(state)

        self._actions.append(
            SignalAction(state=state, signal=signal, data=StateData.copy(data))
        )

    def _submit(self) -> None:
        self._require_open()
        self._closed = True

        if not self._actions:
            self._succeed()
            return

        try:
            self._manager.request_transaction(self)
        except BaseException as error:
            self._manager.abort_transaction(self, error)
            raise

    def _succeed(self) -> None:
        self._finish(None)

    def _fail(self, failure: TransactionFailure) -> None:
        self._finish(failure)

    def _finish(self, failure: TransactionFailure | None) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "StateTransaction must complete on its owning event loop"
            )

        if self._done:
            return

        self._done = True
        self._failure = failure

        self._loop.call_soon(self._dispatch_completion)

    def _dispatch_completion(self) -> None:
        failure = self._failure

        try:
            if failure is None:
                self.dispatch("on_success")
            else:
                self.dispatch("on_failed", failure)
        except Exception:
            logger.exception(
                "State transaction callback failed: id=%s", self.id
            )

        try:
            self.dispatch("on_done", failure)
        except Exception:
            logger.exception(
                "State transaction done callback failed: id=%s", self.id
            )

        if not self._completion.done():
            self._completion.set_result(None)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("State transaction is not accepting mutations")

    def on_success(self) -> None:
        pass

    def on_failed(self, failure: TransactionFailure) -> None:
        pass

    def on_done(self, failure: TransactionFailure | None) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"id={self.id!r}, "
            f"actions={len(self._actions)!r}, "
            f"closed={self.closed!r}, "
            f"done={self.done!r}"
            ")"
        )
