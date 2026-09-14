# src/kivyfn/store/state/manager.py
from __future__ import annotations

import asyncio

from contextvars import ContextVar, Token
from typing import Any, TypeVar, cast, overload

from kivyfn.store.client.client import StoreClient
from kivyfn.store.client.subscription import SubscriptionSource
from kivyfn.store.error import StoreErrorCode, StoreErrorStatus, StoreFailure
from kivyfn.store.protocol.message.event import (
    InsertEventAction,
    RemoveEventAction,
    SetEventAction,
    SetRowEventAction,
    SubscriptionEvent,
)
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    RemoveRequestAction,
    RequestAction,
    SetRequestAction,
    SetRowRequestAction,
    SignalRequestAction,
    SignalRowRequestAction,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    QuerySubscriptionResponse,
    SingleSubscriptionResponse,
    SubscriptionResponse,
    TransactionFailureResponse,
    TransactionResponse,
)
from kivyfn.store.query import Query, QueryRow
from kivyfn.store.query.shape import QueryFieldShape, QueryShape
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.data import StateData
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State
from kivyfn.store.state.registry import StateRegistry
from kivyfn.store.state.transaction.action import (
    InsertAction,
    RemoveAction,
    SetAction,
    SetRowAction,
    SignalAction,
    TransactionAction,
)
from kivyfn.store.state.transaction.context import TransactionContext
from kivyfn.store.state.transaction.failure import TransactionFailure
from kivyfn.store.state.transaction.transaction import StateTransaction


SingleSchemaT = TypeVar("SingleSchemaT", bound=SingleSchema)

ManySchemaT = TypeVar("ManySchemaT", bound=ManySchema)

ReactiveTarget = State[Any] | CollectionState[Any] | QueryCollectionState[Any]

_active_transaction: ContextVar[
    tuple["StateManager", StateTransaction] | None
] = ContextVar("kivyfn_store_active_transaction", default=None)


class StateManager:
    """Coordinate reactive State with one StoreClient."""

    def __init__(self, client: StoreClient) -> None:
        self._client = client
        self._registry = StateRegistry(self)

        self._targets: dict[SubscriptionSource, ReactiveTarget] = {}

        self._subscription_targets: dict[str, ReactiveTarget] = {}

        self._transactions: dict[str, TransactionContext] = {}

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

    def create(self, source: SubscriptionSource) -> ReactiveTarget:
        target = self._registry.create(source)

        self._targets[source] = target

        self._client.subscribe(source, self)

        return target

    def transaction(self) -> StateTransaction:
        return StateTransaction(self)

    def enter_transaction(self, transaction: StateTransaction) -> object:
        active = _active_transaction.get()

        if active is not None:
            raise RuntimeError("Nested State transactions are not supported")

        return _active_transaction.set((self, transaction))

    def leave_transaction(self, token: object) -> None:
        _active_transaction.reset(
            cast(Token[tuple[StateManager, StateTransaction] | None], token)
        )

    def request_transaction(self, transaction: StateTransaction) -> None:
        actions = tuple(
            request
            for action in transaction.actions
            for request in self._request_actions(action)
        )

        context = self._context(transaction)

        self._begin_participants(context)

        future = self._client.transaction(
            transaction_id=transaction.id, actions=actions
        )

        future.add_done_callback(
            lambda completed: self._complete_transaction(
                transaction.id, completed
            )
        )

    def abort_transaction(
        self, transaction: StateTransaction, error: BaseException
    ) -> None:
        context = self._transactions.pop(transaction.id, None)

        self._rollback_transaction(transaction)

        failure = _failure_from_exception(error)

        if context is not None:
            self._fail_participants(context, failure)

        transaction._fail(failure)

    def request_set(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateTransaction:
        if state.row_id is not None:
            raise ValueError("request_set() requires a State without a row ID")

        return self._request(
            lambda transaction: self._set(
                transaction, state, values, optimistic=optimistic, row=False
            )
        )

    def request_setrow(
        self, state: State[Any], values: dict[str, Any], *, optimistic: bool
    ) -> StateTransaction:
        if state.row_id is None:
            raise ValueError("request_setrow() requires a State row")

        return self._request(
            lambda transaction: self._set(
                transaction, state, values, optimistic=optimistic, row=True
            )
        )

    def request_insert(
        self,
        collection: CollectionState[Any],
        row_id: str,
        values: dict[str, Any],
        *,
        optimistic: bool,
    ) -> StateTransaction:
        return self._request(
            lambda transaction: self._insert(
                transaction, collection, row_id, values, optimistic=optimistic
            )
        )

    def request_remove(
        self,
        collection: CollectionState[Any],
        row_id: str,
        *,
        optimistic: bool,
    ) -> StateTransaction:
        return self._request(
            lambda transaction: self._remove(
                transaction, collection, row_id, optimistic=optimistic
            )
        )

    def request_signal(
        self, state: State[Any], signal: str, data: Any
    ) -> StateTransaction:
        return self._request(
            lambda transaction: self._signal(transaction, state, signal, data)
        )

    def bind_subscription(
        self, source: SubscriptionSource, subscription_id: str
    ) -> None:
        target = self._targets.get(source)

        if target is None:
            raise RuntimeError("Subscription has no reactive target")

        self._subscription_targets[subscription_id] = target

    def unbind_subscription(self, subscription_id: str) -> None:
        self._subscription_targets.pop(subscription_id, None)

    def on_subscription_snapshot(
        self, source: SubscriptionSource, response: SubscriptionResponse
    ) -> None:
        target = self._targets.get(source)

        if target is None:
            raise RuntimeError("Subscription has no reactive target")

        if isinstance(response, SingleSubscriptionResponse):
            self._single_snapshot(target, response)

            return

        if isinstance(response, CollectionSubscriptionResponse):
            self._collection_snapshot(target, response)

            return

        if isinstance(response, QuerySubscriptionResponse):
            self._query_snapshot(target, response)

            return

        raise TypeError("Unsupported subscription response")

    def on_subscription_event(self, event: SubscriptionEvent) -> None:
        target = self._subscription_targets.get(event.subscription_id)

        if target is None:
            return

        for action in event.actions:
            if isinstance(action, SetEventAction):
                self._apply_set_event(target, action)

                continue

            if isinstance(action, SetRowEventAction):
                self._apply_setrow_event(target, action)

                continue

            if isinstance(action, InsertEventAction):
                self._apply_insert_event(target, action)

                continue

            if isinstance(action, RemoveEventAction):
                self._apply_remove_event(target, action)

                continue

            raise TypeError("Unsupported subscription event action")

    def on_subscription_status(
        self, source: SubscriptionSource, synced: bool
    ) -> None:
        target = self._targets.get(source)

        if target is None:
            return

        target.synced = synced

    def _request(self, callback: Any) -> StateTransaction:
        active = _active_transaction.get()

        if active is not None:
            manager, transaction = active

            if manager is not self:
                raise RuntimeError(
                    "A State transaction cannot span StateManagers"
                )

            callback(transaction)

            return transaction

        transaction = StateTransaction(self)

        with transaction:
            callback(transaction)

        return transaction

    def _set(
        self,
        transaction: StateTransaction,
        state: State[Any],
        values: dict[str, Any],
        *,
        optimistic: bool,
        row: bool,
    ) -> None:
        copied = StateData.copy_values(values)

        if not copied:
            raise ValueError("State set requires at least one field")

        if row:
            transaction.add_setrow(state, copied)
        else:
            transaction.add_set(state, copied)

        fields = frozenset(copied)

        if optimistic:
            changed = state.set_overlay(transaction.id, copied)
        else:
            changed = state.discard_overlay(transaction.id, fields)

        state.publish(changed)

    def _insert(
        self,
        transaction: StateTransaction,
        collection: CollectionState[Any],
        row_id: str,
        values: dict[str, Any],
        *,
        optimistic: bool,
    ) -> None:
        prepared = _insert_values(collection, values)

        state = self._registry.create_row(type(collection.schema), row_id)

        transaction.add_insert(collection, state, prepared)

        if not optimistic:
            return

        delta = collection.enter(row_id)

        changed = state.set_overlay(transaction.id, prepared)

        state.publish(changed)

        collection.publish_membership(delta)

    def _remove(
        self,
        transaction: StateTransaction,
        collection: CollectionState[Any],
        row_id: str,
        *,
        optimistic: bool,
    ) -> None:
        state = collection[row_id]

        cancelled_insert = transaction.add_remove(collection, state)

        changed = state.discard_overlay(transaction.id)

        state.publish(changed)

        if cancelled_insert:
            delta = collection.leave(row_id)

            collection.publish_membership(delta)

            return

        if not optimistic:
            return

        delta = collection.leave(row_id)

        collection.publish_membership(delta)

    def _signal(
        self,
        transaction: StateTransaction,
        state: State[Any],
        signal: str,
        data: Any,
    ) -> None:
        transaction.add_signal(state, signal, data)

    def _context(self, transaction: StateTransaction) -> TransactionContext:
        context = self._transactions.get(transaction.id)

        if context is not None:
            return context

        states: set[State[Any]] = set()

        collections: set[CollectionState[Any]] = set()

        for action in transaction.actions:
            if isinstance(action, (SetAction, SetRowAction, SignalAction)):
                states.add(action.state)

                continue

            if isinstance(action, (InsertAction, RemoveAction)):
                collections.add(action.collection)

                continue

            raise TypeError("Unsupported State transaction action")

        context = TransactionContext(
            transaction=transaction, states=states, collections=collections
        )

        self._transactions[transaction.id] = context

        return context

    def _begin_participants(self, context: TransactionContext) -> None:
        transaction = context.transaction

        for state in context.states:
            state.begin_transaction(transaction)

        for collection in context.collections:
            collection.begin_transaction(transaction)

    def _succeed_participants(self, context: TransactionContext) -> None:
        transaction = context.transaction

        for state in context.states:
            state.succeed_transaction(transaction)

        for collection in context.collections:
            collection.succeed_transaction(transaction)

    def _fail_participants(
        self, context: TransactionContext, failure: TransactionFailure
    ) -> None:
        transaction = context.transaction

        for state in context.states:
            state.fail_transaction(transaction, failure)

        for collection in context.collections:
            collection.fail_transaction(transaction, failure)

    def _request_actions(
        self, action: TransactionAction
    ) -> tuple[RequestAction, ...]:
        if isinstance(action, SetAction):
            return (
                SetRequestAction(
                    schema=action.state.schema.name,
                    values=tuple(action.values.items()),
                ),
            )

        if isinstance(action, SetRowAction):
            return self._setrow_request_actions(action)

        if isinstance(action, InsertAction):
            return (
                InsertRequestAction(
                    schema=action.collection.schema.name,
                    row_id=_row_id(action.state),
                    values=tuple(action.values.items()),
                ),
            )

        if isinstance(action, RemoveAction):
            return (
                RemoveRequestAction(
                    schema=action.collection.schema.name,
                    row_id=_row_id(action.state),
                ),
            )

        if isinstance(action, SignalAction):
            row_id = action.state.row_id

            if row_id is None:
                return (
                    SignalRequestAction(
                        schema=action.state.schema.name,
                        signal=action.signal,
                        data=action.data,
                    ),
                )

            return (
                SignalRowRequestAction(
                    schema=action.state.schema.name,
                    row_id=row_id,
                    signal=action.signal,
                    data=action.data,
                ),
            )

        raise TypeError("Unsupported State transaction action")

    def _setrow_request_actions(
        self, action: SetRowAction
    ) -> tuple[RequestAction, ...]:
        target = self._query_target(action.state, tuple(action.values))

        if target is None:
            return (
                SetRowRequestAction(
                    schema=action.state.schema.name,
                    row_id=_row_id(action.state),
                    values=tuple(action.values.items()),
                ),
            )

        grouped: dict[tuple[str, str], list[tuple[str, Any]]] = {}

        for name, value in action.values.items():
            field = target.shape.field(name)
            row_id = _query_field_row_id(action.state, target.shape, field)
            key = (field.schema, row_id)
            grouped.setdefault(key, []).append((field.field, value))

        return tuple(
            SetRowRequestAction(
                schema=schema, row_id=row_id, values=tuple(values)
            )
            for (schema, row_id), values in grouped.items()
        )

    def _query_target(
        self, state: State[Any], fields: tuple[str, ...]
    ) -> QueryCollectionState[Any] | None:
        row_id = state.row_id

        if row_id is None:
            return None

        for target in self._targets.values():
            if not isinstance(target, QueryCollectionState):
                continue

            if target.get(row_id) is not state:
                continue

            try:
                for field in fields:
                    target.shape.field(field)
            except KeyError:
                continue

            return target

        return None

    def _complete_transaction(
        self,
        transaction_id: str,
        future: asyncio.Future[
            TransactionResponse | TransactionFailureResponse
        ],
    ) -> None:
        context = self._transactions.pop(transaction_id, None)

        if context is None:
            return

        transaction = context.transaction

        if future.cancelled():
            self._fail_transaction(
                context,
                TransactionFailure(
                    type="network", message="Store transaction was cancelled"
                ),
            )

            return

        try:
            response = future.result()
        except BaseException as error:
            self._fail_transaction(context, _failure_from_exception(error))

            return

        if isinstance(response, TransactionFailureResponse):
            self._fail_transaction(
                context, _transaction_failure(response.failure)
            )

            return

        if not isinstance(response, TransactionResponse):
            self._fail_transaction(
                context,
                TransactionFailure(
                    type="internal",
                    message="Store returned an invalid transaction response",
                ),
            )

            return

        if response.transaction_id != transaction.id:
            self._fail_transaction(
                context,
                TransactionFailure(
                    type="internal",
                    message="Store transaction ID does not match",
                ),
            )

            return

        self._succeed_transaction(context)

    def _succeed_transaction(self, context: TransactionContext) -> None:
        transaction = context.transaction

        for action in transaction.actions:
            if isinstance(action, (SetAction, SetRowAction, InsertAction)):
                changed = action.state.discard_overlay(transaction.id)

                action.state.publish(changed)

        self._succeed_participants(context)

        transaction._succeed()

    def _fail_transaction(
        self, context: TransactionContext, failure: TransactionFailure
    ) -> None:
        self._rollback_transaction(context.transaction)

        self._fail_participants(context, failure)

        context.transaction._fail(failure)

    def _rollback_transaction(self, transaction: StateTransaction) -> None:
        for action in reversed(transaction.actions):
            self._rollback_action(transaction, action)

    def _rollback_action(
        self, transaction: StateTransaction, action: TransactionAction
    ) -> None:
        if isinstance(action, (SetAction, SetRowAction)):
            changed = action.state.discard_overlay(transaction.id)

            action.state.publish(changed)

            return

        if isinstance(action, InsertAction):
            changed = action.state.discard_overlay(transaction.id)

            action.state.publish(changed)

            delta = action.collection.leave(_row_id(action.state))

            action.collection.publish_membership(delta)

            return

        if isinstance(action, RemoveAction):
            delta = action.collection.enter(_row_id(action.state))

            action.collection.publish_membership(delta)

            return

        if isinstance(action, SignalAction):
            return

        raise TypeError("Unsupported State transaction action")

    def _single_snapshot(
        self, target: ReactiveTarget, response: SingleSubscriptionResponse
    ) -> None:
        if not isinstance(target, State):
            raise TypeError("Single subscription target must be State")

        if response.values is None:
            return

        changed = target.set_canonical(dict(response.values))

        target.publish(changed)

    def _collection_snapshot(
        self, target: ReactiveTarget, response: CollectionSubscriptionResponse
    ) -> None:
        if not isinstance(target, CollectionState):
            raise TypeError(
                "Collection subscription target must be CollectionState"
            )

        if response.rows is None:
            return

        self._rows_snapshot(target, response.rows)

    def _query_snapshot(
        self, target: ReactiveTarget, response: QuerySubscriptionResponse
    ) -> None:
        if not isinstance(target, QueryCollectionState):
            raise TypeError(
                "Query subscription target must be QueryCollectionState"
            )

        if response.rows is None:
            return

        self._rows_snapshot(target, response.rows)

    def _rows_snapshot(
        self,
        target: CollectionState[Any] | QueryCollectionState[Any],
        rows: tuple[QueryRow, ...],
    ) -> None:
        delta = target.set_membership(tuple(row.id for row in rows))

        for row in rows:
            state = target[row.id]

            changed = state.set_canonical(row.to_dict())

            state.publish(changed)

        target.publish_membership(delta)

    def _apply_set_event(
        self, target: ReactiveTarget, action: SetEventAction
    ) -> None:
        if not isinstance(target, State):
            raise TypeError("Set event requires a State target")

        changed = target.set_canonical(dict(action.values))

        target.publish(changed)

    def _apply_setrow_event(
        self, target: ReactiveTarget, action: SetRowEventAction
    ) -> None:
        if isinstance(target, State):
            raise TypeError("SetRow event requires a collection target")

        state = target.get(action.row_id)

        if state is None:
            raise RuntimeError(
                f"SetRow event references an unknown row: {action.row_id!r}"
            )

        changed = state.set_canonical(dict(action.values))

        state.publish(changed)

    def _apply_insert_event(
        self, target: ReactiveTarget, action: InsertEventAction
    ) -> None:
        if isinstance(target, State):
            raise TypeError("Insert event requires a collection target")

        delta = target.enter(action.row_id)

        state = target[action.row_id]

        changed = state.set_canonical(dict(action.values))

        state.publish(changed)

        target.publish_membership(delta)

        if isinstance(target, CollectionState) and not isinstance(
            target, QueryCollectionState
        ):
            target.publish_insert(action.row_id, state)

    def _apply_remove_event(
        self, target: ReactiveTarget, action: RemoveEventAction
    ) -> None:
        if isinstance(target, State):
            raise TypeError("Remove event requires a collection target")

        if isinstance(target, CollectionState) and not isinstance(
            target, QueryCollectionState
        ):
            state = self._registry.create_row(
                type(target.schema), action.row_id
            )
        else:
            state = target.get(action.row_id)

            if state is None:
                raise RuntimeError(
                    f"Remove event references an unknown row: {action.row_id!r}"
                )

        delta = target.leave(action.row_id)

        target.publish_membership(delta)

        if isinstance(target, CollectionState) and not isinstance(
            target, QueryCollectionState
        ):
            target.publish_remove(action.row_id, state)


def _query_field_row_id(
    state: State[Any], shape: QueryShape, field: QueryFieldShape
) -> str:
    if not field.relation_path:
        return _row_id(state)

    join = next(
        (
            join
            for join in shape.joins
            if join.relation_path == field.relation_path
        ),
        None,
    )

    if join is None:
        raise RuntimeError(
            f"Query field {field.name!r} has no matching join relationship"
        )

    owner_field = next(
        (
            candidate
            for candidate in shape.fields
            if candidate.relation_path == join.owner_path
            and candidate.field == join.field_name
        ),
        None,
    )

    if owner_field is None:
        raise RuntimeError(
            f"Query join {'.'.join(join.relation_path)!r} has no owner "
            "foreign-key field"
        )

    row_id = getattr(state, owner_field.name)

    if row_id is None:
        raise ValueError(
            f"Cannot write query field {field.name!r}: relationship "
            f"{'.'.join(field.relation_path)!r} has no target row"
        )

    return str(row_id)


def _insert_values(
    collection: CollectionState[Any], values: dict[str, Any]
) -> dict[str, Any]:
    prepared = collection.schema.defaults()

    prepared.update(StateData.copy_values(values))

    return StateData.copy_values(prepared)


def _row_id(state: State[Any]) -> str:
    row_id = state.row_id

    if row_id is None:
        raise RuntimeError("State row ID is required")

    return row_id


def _transaction_failure(failure: StoreFailure) -> TransactionFailure:
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

    return TransactionFailure(type=failure_type, message=failure.message)


def _failure_from_exception(error: BaseException) -> TransactionFailure:
    if isinstance(error, asyncio.TimeoutError):
        failure_type = "timeout"

    elif isinstance(error, ConnectionError):
        failure_type = "network"

    else:
        failure_type = "internal"

    message = str(error)

    if not message:
        message = type(error).__name__

    return TransactionFailure(type=failure_type, message=message)
