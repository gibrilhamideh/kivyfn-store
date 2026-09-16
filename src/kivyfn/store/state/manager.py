# src/kivyfn/store/state/manager.py
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar, Token
from functools import partial
from typing import Any, TypeVar, cast, overload

from kivyfn.store.client.client import StoreClient
from kivyfn.store.client.subscription import (
    SubscriptionPublication,
    SubscriptionSource,
    SubscriptionUpdate,
)
from kivyfn.store.error import (
    StoreErrorCode,
    StoreErrorStatus,
    StoreFailure,
    StoreTimeoutError,
    StoreUnavailableError,
)
from kivyfn.store.protocol.message.event import (
    SignalEventAction,
    SignalRowEventAction,
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
from kivyfn.store.query import Query
from kivyfn.store.query.shape import QueryFieldShape, QueryShape
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.data import StateData
from kivyfn.store.state.reactive.dispatch import (
    collect_notifications,
    dispatch,
    notification_batch,
)
from kivyfn.store.state.reactive.query import QueryCollectionState
from kivyfn.store.state.reactive.state import State
from kivyfn.store.state.registry import StateRegistry
from kivyfn.store.state.template import StateTemplate, TargetT
from kivyfn.store.state.transaction.action import (
    InsertAction,
    RemovalOutcome,
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
    """Route subscription updates and coordinate local transactions.

    Reactive targets own their values, membership, and notifications. This
    manager owns reactive targets, transaction submission/completion,
    and resolving projected field writes to canonical records.
    """

    def __init__(self, client: StoreClient) -> None:
        self._client = client
        self._registry = StateRegistry(self)

        self._targets: dict[SubscriptionSource, ReactiveTarget] = {}

        self._transactions: dict[str, TransactionContext] = {}
        self._snapshot_handlers: dict[type, Callable[..., None]] = {
            SingleSubscriptionResponse: self._single_snapshot,
            CollectionSubscriptionResponse: self._collection_snapshot,
            QuerySubscriptionResponse: self._query_snapshot,
        }
        self._request_handlers: dict[
            type, Callable[..., tuple[RequestAction, ...]]
        ] = {
            SetAction: self._set_request_actions,
            SetRowAction: self._setrow_request_actions,
            InsertAction: self._insert_request_actions,
            RemoveAction: self._remove_request_actions,
            SignalAction: self._signal_request_actions,
        }

    @property
    def client(self) -> StoreClient:
        return self._client

    @overload
    def create(self, source: StateTemplate[TargetT]) -> TargetT: ...

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
        self, source: SubscriptionSource | StateTemplate[Any]
    ) -> ReactiveTarget:
        target = self._registry.create(source)
        if isinstance(source, StateTemplate):
            source = source.source

        self._targets[source] = target

        self._client.subscribe(source, self)

        return target

    def release(self, source: SubscriptionSource | StateTemplate[Any]) -> None:
        """Release a subscription and its synchronization ownership."""
        if isinstance(source, StateTemplate):
            source = source.source
        target = self._targets.pop(source, None)
        if target is None:
            return
        self._client.unsubscribe(source, self)
        target.set_synced(False)

    def close(self) -> None:
        """Release subscriptions and finish outstanding local operations."""
        for context in tuple(self._transactions.values()):
            self.abort_transaction(
                context.transaction, ConnectionError("StateManager closed")
            )
        for source in tuple(self._targets):
            self.release(source)

    def transaction(
        self,
        *,
        history_group: str | None = None,
        history_label: str | None = None,
    ) -> StateTransaction:
        return StateTransaction(
            self, history_group=history_group, history_label=history_label
        )

    def enter_transaction(self, transaction: StateTransaction) -> object:
        active = _active_transaction.get()

        if active is not None and not active[1].closed:
            raise RuntimeError("Nested State transactions are not supported")

        return _active_transaction.set((self, transaction))

    def leave_transaction(self, token: object) -> None:
        _active_transaction.reset(
            cast(Token[tuple[StateManager, StateTransaction] | None], token)
        )

    def request_transaction(self, transaction: StateTransaction) -> None:
        """Submit recorded intent, then settle participants from the reply.

        Canonical events arrive through subscriptions before the response
        future completes. The StateTransaction has its own completion future.
        Pending callbacks may abort it before any request is submitted.
        """
        actions = tuple(
            request
            for action in transaction.actions
            for request in self._request_actions(action)
        )

        context = self._context(transaction)

        if not self._begin_participants(context):
            return

        future = self._client.transaction(
            transaction_id=transaction.id,
            actions=actions,
            history_group=transaction.history_group,
            history_label=transaction.history_label,
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

        self._discard_transaction_overlays(transaction)

        failure = _failure_from_exception(error)

        transaction._fail(failure)
        if context is not None:
            self._fail_participants(context, failure)

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

    def on_subscription_snapshot(
        self, source: SubscriptionSource, response: SubscriptionResponse
    ) -> None:
        target = self._targets.get(source)
        if target is None:
            raise RuntimeError("Subscription has no reactive target")
        handler = self._snapshot_handlers.get(type(response))
        if handler is None:
            raise TypeError("Unsupported subscription response")
        handler(target, response)

    def prepare_subscription_updates(
        self,
        updates: tuple[SubscriptionUpdate, ...],
        *,
        transaction_id: str | None = None,
    ) -> SubscriptionPublication:
        """Stage every target and retire the matching optimistic overlays.

        The client resolves server IDs to sources and checks revisions first.
        It prepares all affected managers before publishing these callbacks,
        so bindings and signals see the full commit.
        """
        signals: list[
            tuple[ReactiveTarget, SignalEventAction | SignalRowEventAction]
        ] = []
        with collect_notifications() as batch:
            for update in updates:
                target = self._targets.get(update.source)
                if target is None:
                    continue
                for action in update.actions:
                    if isinstance(
                        action, (SignalEventAction, SignalRowEventAction)
                    ):
                        signals.append((target, action))
                    else:
                        target.apply_event(action)

            if transaction_id is not None:
                context = self._transactions.get(transaction_id)
                if context is not None:
                    self._discard_transaction_overlays(context.transaction)

        occurrences = []
        seen: set[tuple[State[Any], int]] = set()
        for target, action in signals:
            if isinstance(action, SignalEventAction):
                if not isinstance(target, State):
                    raise TypeError("Signal event requires a State target")
                emit = partial(
                    dispatch,
                    target,
                    f"on_{action.signal}",
                    StateData.copy(action.data),
                )
            else:
                if isinstance(target, State):
                    raise TypeError("SignalRow event requires a collection")
                state = target.canonical_row(action.row_id)
                if state is None:
                    raise RuntimeError(
                        "Signal references an unknown canonical row"
                    )
                emit = partial(
                    self._publish_row_signal, target, state, action, seen
                )
            occurrences.append((action.occurrence_id, emit))
        return SubscriptionPublication(batch.publish, tuple(occurrences))

    def _publish_row_signal(
        self,
        target: CollectionState[Any] | QueryCollectionState[Any],
        state: State[Any],
        action: SignalRowEventAction,
        seen: set[tuple[State[Any], int]],
    ) -> None:
        occurrence = action.occurrence_id
        if occurrence is None or (state, occurrence) not in seen:
            dispatch(state, f"on_{action.signal}", StateData.copy(action.data))
            if occurrence is not None:
                seen.add((state, occurrence))
        dispatch(
            target,
            f"on_{action.signal}",
            action.row_id,
            StateData.copy(action.data),
        )

    def on_subscription_status(
        self, source: SubscriptionSource, synced: bool
    ) -> None:
        """Reflect subscription readiness while retaining stored values."""
        target = self._targets.get(source)

        if target is None:
            return

        target.set_synced(synced)

    def _request(self, callback: Any) -> StateTransaction:
        active = _active_transaction.get()

        if active is not None:
            manager, transaction = active
            if transaction.closed:
                active = None

        if active is not None:
            manager, transaction = active
            if transaction.owner_task is not asyncio.current_task():
                raise RuntimeError(
                    "State transaction belongs to another asyncio task"
                )
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
            writes = self._resolve_row_writes(transaction, state, copied)
            transaction.add_setrow(state, copied, writes)
        else:
            transaction.add_set(state, copied)

        if optimistic:
            state.set_overlay(transaction.id, copied)
        else:
            state.discard_overlay(transaction.id, frozenset(copied))

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

        collection.apply_insert_overlay(transaction.id, state, prepared)

    def _remove(
        self,
        transaction: StateTransaction,
        collection: CollectionState[Any],
        row_id: str,
        *,
        optimistic: bool,
    ) -> None:
        state = collection[row_id]
        outcome = transaction.add_remove(collection, state)

        with notification_batch():
            state.discard_overlay(transaction.id)
            if outcome is RemovalOutcome.CANCELLED_INSERT:
                collection.discard_overlay(transaction.id, (row_id,))
            elif outcome is RemovalOutcome.CANCELLED_REPLACEMENT or optimistic:
                collection.set_overlay(transaction.id, {row_id: None})

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

    def _begin_participants(self, context: TransactionContext) -> bool:
        """Start participants and report whether submission may proceed.

        A callback can close the manager while a participant starts. Check
        completion before each next participant and before client submission.
        """
        transaction = context.transaction
        for participant in (*context.states, *context.collections):
            if transaction.done:
                return False
            participant.begin_transaction(transaction)
        return not transaction.done

    def _succeed_participants(self, context: TransactionContext) -> None:
        for participant in (*context.states, *context.collections):
            participant.succeed_transaction(context.transaction)

    def _fail_participants(
        self, context: TransactionContext, failure: TransactionFailure
    ) -> None:
        for participant in (*context.states, *context.collections):
            participant.fail_transaction(context.transaction, failure)

    def _request_actions(
        self, action: TransactionAction
    ) -> tuple[RequestAction, ...]:
        """Encode local intent, expanding projected writes into requests."""
        handler = self._request_handlers.get(type(action))
        if handler is None:
            raise TypeError("Unsupported State transaction action")
        return handler(action)

    def _set_request_actions(
        self, action: SetAction
    ) -> tuple[RequestAction, ...]:
        return (
            SetRequestAction(
                schema=action.state.schema.name,
                values=tuple(action.values.items()),
            ),
        )

    def _insert_request_actions(
        self, action: InsertAction
    ) -> tuple[RequestAction, ...]:
        return (
            InsertRequestAction(
                schema=action.collection.schema.name,
                row_id=_row_id(action.state),
                values=tuple(action.values.items()),
            ),
        )

    def _remove_request_actions(
        self, action: RemoveAction
    ) -> tuple[RequestAction, ...]:
        return (
            RemoveRequestAction(
                schema=action.collection.schema.name,
                row_id=_row_id(action.state),
            ),
        )

    def _signal_request_actions(
        self, action: SignalAction
    ) -> tuple[RequestAction, ...]:
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

    def _setrow_request_actions(
        self, action: SetRowAction
    ) -> tuple[RequestAction, ...]:
        grouped: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        for (schema, row_id, field), value in action.writes.items():
            grouped.setdefault((schema, row_id), []).append((field, value))
        return tuple(
            SetRowRequestAction(
                schema=schema, row_id=row_id, values=tuple(values)
            )
            for (schema, row_id), values in grouped.items()
        )

    def _resolve_row_writes(
        self,
        transaction: StateTransaction,
        state: State[Any],
        values: dict[str, Any],
    ) -> dict[tuple[str, str, str], Any]:
        # Resolve now, while the transaction's relationship writes are known.
        # Re-resolving at response time could target a different joined row.
        shape = cast(QueryShape | None, getattr(type(state), "_query_shape"))
        if shape is None:
            return {
                (state.schema.name, _row_id(state), field): value
                for field, value in values.items()
            }
        previous: dict[tuple[str, str, str], Any] = {}
        for action in transaction.actions:
            if isinstance(action, SetRowAction):
                previous.update(action.writes)
        result: dict[tuple[str, str, str], Any] = {}
        # Relationship fields in this call are logical writes even for .set().
        fields = sorted(
            values, key=lambda name: len(shape.field(name).relation_path)
        )
        for name in fields:
            field = shape.field(name)
            row_id = self._query_field_row_id(state, shape, field, previous)
            key = (field.schema, row_id, field.field)
            result[key] = previous[key] = values[name]
        return result

    def _query_field_row_id(
        self,
        state: State[Any],
        shape: QueryShape,
        field: QueryFieldShape,
        writes: dict[tuple[str, str, str], Any],
    ) -> str:
        row_id = original_id = _row_id(state)
        canonical = state.canonical_snapshot(frozenset(state.fields))
        for depth in range(1, len(field.relation_path) + 1):
            path = field.relation_path[:depth]
            join = next(
                join for join in shape.joins if join.relation_path == path
            )
            owner = next(
                candidate
                for candidate in shape.fields
                if candidate.relation_path == join.owner_path
                and candidate.field == join.field_name
            )
            key = (owner.schema, row_id, owner.field)
            original_next = canonical[owner.name]
            if key in writes:
                next_id = writes[key]
            elif row_id == original_id:
                next_id = original_next
            else:
                related = self._registry.find_row(owner.schema, row_id)
                if related is None or not related.synced:
                    raise ValueError(
                        "Cannot resolve nested joined write "
                        "after retargeting: "
                        f"{field.name!r}; subscribe to {owner.schema} first"
                    )
                next_id = related.canonical_snapshot({owner.field})[
                    owner.field
                ]
            if next_id is None:
                raise ValueError(
                    f"Cannot write query field {field.name!r}: "
                    f"relationship {'.'.join(path)!r} has no target row"
                )
            row_id = str(next_id)
            original_id = original_next
        return row_id

    def _complete_transaction(
        self,
        transaction_id: str,
        future: asyncio.Future[
            TransactionResponse | TransactionFailureResponse
        ],
    ) -> None:
        """Settle local lifecycle from the client's matched reply or error.

        The client validates response types and IDs before resolving futures.
        Canonical events have already been delivered when success arrives.
        """
        context = self._transactions.pop(transaction_id, None)

        if context is None:
            return

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

        self._succeed_transaction(context)

    def _succeed_transaction(self, context: TransactionContext) -> None:
        self._discard_transaction_overlays(context.transaction)
        context.transaction._succeed()
        self._succeed_participants(context)

    def _fail_transaction(
        self, context: TransactionContext, failure: TransactionFailure
    ) -> None:
        self._discard_transaction_overlays(context.transaction)
        context.transaction._fail(failure)
        self._fail_participants(context, failure)

    def _discard_transaction_overlays(
        self, transaction: StateTransaction
    ) -> None:
        """Retire only this transaction after commit, failure, or abort."""
        states: set[State[Any]] = set()
        collections: set[CollectionState[Any]] = set()
        for action in transaction.actions:
            states.add(action.state)
            if isinstance(action, (InsertAction, RemoveAction)):
                collections.add(action.collection)
        with notification_batch():
            for state in states:
                state.discard_overlay(transaction.id)
            for collection in collections:
                collection.discard_overlay(transaction.id)

    def _single_snapshot(
        self, target: ReactiveTarget, response: SingleSubscriptionResponse
    ) -> None:
        if not isinstance(target, State):
            raise TypeError("Single subscription target must be State")

        if response.values is None:
            return

        target.set_canonical(dict(response.values))

    def _collection_snapshot(
        self, target: ReactiveTarget, response: CollectionSubscriptionResponse
    ) -> None:
        if not isinstance(target, CollectionState):
            raise TypeError(
                "Collection subscription target must be CollectionState"
            )

        if response.rows is None:
            return

        target.apply_snapshot(response.rows)

    def _query_snapshot(
        self, target: ReactiveTarget, response: QuerySubscriptionResponse
    ) -> None:
        if not isinstance(target, QueryCollectionState):
            raise TypeError(
                "Query subscription target must be QueryCollectionState"
            )

        if response.rows is None:
            return

        target.apply_snapshot(response.rows)


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
    if isinstance(error, (asyncio.TimeoutError, StoreTimeoutError)):
        failure_type = "timeout"

    elif isinstance(error, (ConnectionError, StoreUnavailableError)):
        failure_type = "network"

    else:
        failure_type = "internal"

    message = str(error)

    if not message:
        message = type(error).__name__

    return TransactionFailure(type=failure_type, message=message)
