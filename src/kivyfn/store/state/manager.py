# src/kivyfn/store/state/manager.py
from __future__ import annotations

import asyncio

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, TypeVar, cast, overload

from kivyfn.store.client.client import StoreClient
from kivyfn.store.error import StoreErrorCode, StoreErrorStatus, StoreFailure
from kivyfn.store.protocol.message.responses import (
    FailureResponse,
    ResponseItem,
    SetResponse,
    SetRowResponse,
)
from kivyfn.store.query import Query
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.state.mutation.failure import StateFailure
from kivyfn.store.state.mutation.operation import StateOperation
from kivyfn.store.state.mutation.transaction import (
    InsertAction,
    RemoveAction,
    SetAction,
    SetRowAction,
    SignalAction,
    StateAction,
    StateTransaction,
)
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.data import StateData
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State
from kivyfn.store.state.registry import StateRegistry


SingleSchemaT = TypeVar("SingleSchemaT", bound=SingleSchema)

ManySchemaT = TypeVar("ManySchemaT", bound=ManySchema)


@dataclass(slots=True)
class _SetContext:
    state: State[Any]
    operation: StateOperation
    values: dict[str, Any]
    old: dict[str, Any]


class StateManager:
    """Application-facing coordinator for reactive Store State."""

    def __init__(self, client: StoreClient) -> None:
        self._client = client
        self._registry = StateRegistry(self)

        self._sets: dict[str, _SetContext] = {}

        self._transaction: ContextVar[StateTransaction | None] = ContextVar(
            f"kivyfn_state_transaction_{id(self)}", default=None
        )

    @property
    def client(self) -> StoreClient:
        return self._client

    @overload
    def create(self, source: type[SingleSchemaT]) -> State[SingleSchemaT]: ...

    @overload
    def create(
        self, source: type[ManySchemaT]
    ) -> CollectionState[ManySchemaT]: ...

    @overload
    def create(
        self, source: Query[ManySchemaT]
    ) -> QueryCollectionState[ManySchemaT]: ...

    def create(
        self, source: (type[SingleSchema] | type[ManySchema] | Query[Any])
    ) -> Any:
        return self._registry.create(source)

    def transaction(self) -> StateTransaction:
        return StateTransaction(self)

    def enter_transaction(self, transaction: StateTransaction) -> object:
        if self._transaction.get() is not None:
            raise RuntimeError("Nested State transactions are not supported")

        return self._transaction.set(transaction)

    def leave_transaction(self, token: object) -> None:
        self._transaction.reset(cast(Token[StateTransaction | None], token))

    def request_transaction(self, transaction: StateTransaction) -> None:
        raise NotImplementedError(
            "StoreClient transaction request is the next branch"
        )

    def abort_transaction(
        self, transaction: StateTransaction, error: BaseException
    ) -> None:
        self._rollback_transaction(transaction)

        failure = _failure_from_exception(error)

        self._fail_transaction_participants(transaction, failure)

        transaction.operation.fail(failure)

    def request_set(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateOperation:
        if state.row_id is not None:
            raise ValueError("request_set() requires a State without a row ID")

        transaction = self._transaction.get()

        if transaction is not None:
            return self._transaction_set(transaction, state, values)

        return self._request_set(
            state, values, optimistic=optimistic, row=False
        )

    def request_setrow(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateOperation:
        if state.row_id is None:
            raise ValueError("request_setrow() requires a State row ID")

        transaction = self._transaction.get()

        if transaction is not None:
            return self._transaction_setrow(transaction, state, values)

        return self._request_set(
            state, values, optimistic=optimistic, row=True
        )

    def request_insert(
        self,
        collection: CollectionState[Any],
        row_id: str,
        values: dict[str, Any],
        *,
        optimistic: bool,
    ) -> StateOperation:
        transaction = self._transaction.get()

        if transaction is not None:
            return self._transaction_insert(
                transaction, collection, row_id, values
            )

        raise NotImplementedError(
            "StoreClient insert request is the next collection branch"
        )

    def request_remove(
        self,
        collection: CollectionState[Any],
        row_id: str,
        *,
        optimistic: bool,
    ) -> StateOperation:
        transaction = self._transaction.get()

        if transaction is not None:
            return self._transaction_remove(transaction, collection, row_id)

        raise NotImplementedError(
            "StoreClient remove request is the next collection branch"
        )

    def request_signal(
        self, state: State[Any], signal: str, data: Any
    ) -> StateOperation:
        transaction = self._transaction.get()

        if transaction is not None:
            return self._transaction_signal(transaction, state, signal, data)

        raise NotImplementedError(
            "StoreClient signal request is the next signal branch"
        )

    def _request_set(
        self,
        state: State[Any],
        values: dict[str, Any],
        *,
        optimistic: bool,
        row: bool,
    ) -> StateOperation:
        if not values:
            raise ValueError("State set requires at least one field")

        copied = StateData.copy_values(values)
        fields = frozenset(copied)
        old = state.visible_snapshot(fields)

        operation = StateOperation("set", optimistic=optimistic)
        context = _SetContext(
            state=state, operation=operation, values=copied, old=old
        )

        self._sets[operation.id] = context

        if optimistic:
            changed = state.stage_overlay(operation, copied)
            state.publish(changed)

        for key, value in copied.items():
            state.begin_operation(operation, key, old.get(key), value)

        try:
            if row:
                future = self._client.setrow(
                    operation_id=operation.id,
                    schema=state.schema.name,
                    row_id=cast(str, state.row_id),
                    values=copied,
                )
            else:
                future = self._client.set(
                    operation_id=operation.id,
                    schema=state.schema.name,
                    values=copied,
                )
        except BaseException as error:
            self._sets.pop(operation.id, None)
            self._fail_set(context, _failure_from_exception(error))
            return operation

        future.add_done_callback(
            lambda completed: self._complete_set(
                operation.id, completed, row=row
            )
        )

        return operation

    def _transaction_set(
        self,
        transaction: StateTransaction,
        state: State[Any],
        values: dict[str, Any],
    ) -> StateOperation:
        copied, old = _set_values(state, values)

        transaction.add_set(state, old, copied)
        self._stage_transaction_set(transaction, state, old, copied)

        return transaction.operation

    def _transaction_setrow(
        self,
        transaction: StateTransaction,
        state: State[Any],
        values: dict[str, Any],
    ) -> StateOperation:
        copied, old = _set_values(state, values)

        transaction.add_setrow(state, old, copied)
        self._stage_transaction_set(transaction, state, old, copied)

        return transaction.operation

    def _stage_transaction_set(
        self,
        transaction: StateTransaction,
        state: State[Any],
        old: dict[str, Any],
        new: dict[str, Any],
    ) -> None:
        changed = state.stage_overlay(transaction.operation, new)
        state.publish(changed)

        for key, value in new.items():
            state.begin_operation(
                transaction.operation, key, old.get(key), value
            )

    def _transaction_insert(
        self,
        transaction: StateTransaction,
        collection: CollectionState[Any],
        row_id: str,
        values: dict[str, Any],
    ) -> StateOperation:
        prepared = _record_values(collection, values)

        delta = collection.stage_enter(row_id)
        state = collection[row_id]

        transaction.add_insert(collection, state, prepared)

        changed = state.stage_overlay(transaction.operation, prepared)
        state.publish(changed)

        collection.publish_membership(delta)
        collection.begin_operation(
            transaction.operation, row_id, None, prepared
        )

        return transaction.operation

    def _transaction_remove(
        self,
        transaction: StateTransaction,
        collection: CollectionState[Any],
        row_id: str,
    ) -> StateOperation:
        state = collection[row_id]
        fields = frozenset(state.fields)
        old = state.visible_snapshot(fields)

        cancelled_insert = transaction.add_remove(collection, state, old)

        changed = state.discard_overlay(transaction.operation)
        state.publish(changed)

        delta = collection.stage_leave(row_id)
        collection.publish_membership(delta)

        if not cancelled_insert:
            collection.begin_operation(
                transaction.operation, row_id, old, None
            )

        return transaction.operation

    def _transaction_signal(
        self,
        transaction: StateTransaction,
        state: State[Any],
        signal: str,
        data: Any,
    ) -> StateOperation:
        copied = StateData.copy(data)

        transaction.add_signal(state, signal, copied)
        state.begin_operation(transaction.operation, signal, None, copied)

        return transaction.operation

    def _rollback_transaction(self, transaction: StateTransaction) -> None:
        for action in reversed(transaction.actions):
            self._rollback_action(transaction, action)

    def _rollback_action(
        self, transaction: StateTransaction, action: StateAction
    ) -> None:
        operation = transaction.operation

        if isinstance(action, (SetAction, SetRowAction)):
            changed = action.state.discard_overlay(operation)
            action.state.publish(changed)
            return

        if isinstance(action, InsertAction):
            changed = action.state.discard_overlay(operation)
            action.state.publish(changed)

            delta = action.collection.stage_leave(
                cast(str, action.state.row_id)
            )
            action.collection.publish_membership(delta)
            return

        if isinstance(action, RemoveAction):
            row_id = cast(str, action.state.row_id)
            delta = action.collection.stage_enter(row_id)
            action.collection.publish_membership(delta)
            return

        if isinstance(action, SignalAction):
            return

        raise TypeError("Unsupported State transaction action")

    def _fail_transaction_participants(
        self, transaction: StateTransaction, failure: StateFailure
    ) -> None:
        operation = transaction.operation

        states: set[State[Any]] = set()
        collections: set[CollectionState[Any]] = set()

        for action in transaction.actions:
            if isinstance(action, (SetAction, SetRowAction, SignalAction)):
                states.add(action.state)
                continue

            collections.add(action.collection)

        for state in states:
            state.fail_operation(operation, None, None, None, failure)

        for collection in collections:
            collection.fail_operation(operation, "", None, None, failure)

    def _complete_set(
        self,
        operation_id: str,
        future: asyncio.Future[ResponseItem],
        *,
        row: bool,
    ) -> None:
        context = self._sets.pop(operation_id, None)

        if context is None:
            return

        if future.cancelled():
            self._fail_set(
                context,
                StateFailure(
                    type="network", message="Store set was cancelled"
                ),
            )
            return

        try:
            response = future.result()
        except BaseException as error:
            self._fail_set(context, _failure_from_exception(error))
            return

        if isinstance(response, FailureResponse):
            self._fail_set(
                context,
                _state_failure(response.failure),
                key=_failure_key(response.failure, context),
            )
            return

        expected = SetRowResponse if row else SetResponse

        if not isinstance(response, expected):
            self._fail_set(
                context,
                StateFailure(
                    type="internal",
                    message="Store returned an invalid set response",
                ),
            )
            return

        if response.operation_id != context.operation.id:
            self._fail_set(
                context,
                StateFailure(
                    type="internal",
                    message="Store set operation ID does not match",
                ),
            )
            return

        canonical = dict(response.values)
        missing = set(context.values) - set(canonical)

        if missing:
            self._fail_set(
                context,
                StateFailure(
                    type="internal",
                    message="Store set response is missing fields",
                ),
            )
            return

        self._succeed_set(context, canonical)

    def _succeed_set(
        self, context: _SetContext, canonical: dict[str, Any]
    ) -> None:
        state = context.state
        operation = context.operation

        changed = set(state.stage_canonical(canonical))
        changed.update(state.discard_overlay(operation))

        state.publish(changed)

        for key in context.values:
            state.succeed_operation(
                operation, key, context.old.get(key), canonical.get(key)
            )

        operation.succeed()

    def _fail_set(
        self,
        context: _SetContext,
        failure: StateFailure,
        *,
        key: str | None = None,
    ) -> None:
        state = context.state
        operation = context.operation

        changed = state.discard_overlay(operation)
        state.publish(changed)

        if key is None and len(context.values) == 1:
            key = next(iter(context.values))

        old = context.old.get(key) if key is not None else None
        new = context.values.get(key) if key is not None else None

        state.fail_operation(operation, key, old, new, failure)
        operation.fail(failure)


def _set_values(
    state: State[Any], values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not values:
        raise ValueError("State set requires at least one field")

    copied = StateData.copy_values(values)
    old = state.visible_snapshot(frozenset(copied))

    return copied, old


def _record_values(
    collection: CollectionState[Any], values: dict[str, Any]
) -> dict[str, Any]:
    prepared = collection.schema.defaults()
    prepared.update(StateData.copy_values(values))
    collection.schema.challenge_fields(prepared)

    return StateData.copy_values(prepared)


def _failure_key(failure: StoreFailure, context: _SetContext) -> str | None:
    store_context = failure.context

    if store_context is not None:
        field = store_context.field

        if field in context.values:
            return field

    return None


def _state_failure(failure: StoreFailure) -> StateFailure:
    source = failure.context.source if failure.context is not None else None

    if source == "trigger":
        failure_type = "trigger"
    elif source == "policy":
        failure_type = "policy"
    elif failure.code in {
        StoreErrorCode.FIELD_NOT_FOUND,
        StoreErrorCode.FIELD_VALIDATION_FAILED,
    }:
        failure_type = "field"
    elif failure.code in {
        StoreErrorCode.FOREIGN_KEY_NOT_FOUND,
        StoreErrorCode.FOREIGN_KEY_RESTRICTED,
    }:
        failure_type = "foreign_key"
    elif failure.status == StoreErrorStatus.NOT_FOUND:
        failure_type = "not_found"
    elif failure.status == StoreErrorStatus.CONFLICT:
        failure_type = "conflict"
    elif failure.status == StoreErrorStatus.TIMEOUT:
        failure_type = "timeout"
    elif failure.status == StoreErrorStatus.UNAVAILABLE:
        failure_type = "network"
    else:
        failure_type = "internal"

    return StateFailure(type=failure_type, message=failure.message)


def _failure_from_exception(error: BaseException) -> StateFailure:
    if isinstance(error, asyncio.TimeoutError):
        failure_type = "timeout"
    elif isinstance(error, ConnectionError):
        failure_type = "network"
    else:
        failure_type = "internal"

    message = str(error)

    if not message:
        message = type(error).__name__

    return StateFailure(type=failure_type, message=message)
