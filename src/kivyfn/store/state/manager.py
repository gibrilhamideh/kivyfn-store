from __future__ import annotations

import asyncio
import contextvars

from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, TypeVar, cast, overload
from weakref import WeakValueDictionary

from kivy.properties import BooleanProperty

from kivyfn.logging import get_logger
from kivyfn.store.address import Address
from kivyfn.store.error import (
    StoreOperationError,
    StoreStateError,
)
from kivyfn.store.query import (
    Query,
    QueryChangeSet,
    QueryInsertEvent,
    QueryRemoveEvent,
    QueryUpdateEvent,
)
from kivyfn.store.schema import (
    ManySchema,
    Schema,
    SingleSchema,
)
from kivyfn.store.state.base import StateEvents
from kivyfn.store.state.collection import CollectionState
from kivyfn.store.state.entity import EntityState, entity_type
from kivyfn.store.state.operation import (
    StateOperation,
    StateOperationKind,
    StateResult,
)
from kivyfn.store.state.transaction import StateTransaction
from kivyfn.store.state.view import SingleStateView
from kivyfn.store.signals.event import SignalEvent


logger = get_logger(__name__)


SchemaT = TypeVar(
    "SchemaT",
    bound=Schema,
)

SingleSchemaT = TypeVar(
    "SingleSchemaT",
    bound=SingleSchema,
)

ManySchemaT = TypeVar(
    "ManySchemaT",
    bound=ManySchema,
)


View = CollectionState[Any] | SingleStateView[Any]

Dispatcher = Callable[..., None]


class BaseStateManager(StateEvents, ABC):
    """Shared live-state API with local and remote backends."""

    connected = BooleanProperty(True)

    def __init__(
        self,
        *,
        dispatcher: Dispatcher | None = None,
        **kwargs: Any,
    ) -> None:
        self._dispatcher = dispatcher

        self._schemas: dict[str, Schema] = {}
        self._schema_types: dict[str, type[Schema]] = {}
        self._addresses: dict[str, Address[Any]] = {}

        self._entities: WeakValueDictionary[
            tuple[str, str | None],
            EntityState[Any],
        ] = WeakValueDictionary()

        self._views: set[View] = set()
        self._direct_views: dict[str, View] = {}

        self._entity_views: dict[
            EntityState[Any],
            set[View],
        ] = defaultdict(set)

        self._view_entities: dict[
            View,
            set[EntityState[Any]],
        ] = defaultdict(set)

        self._signal_bindings: dict[
            tuple[str, str],
            Any,
        ] = {}

        self._active_transaction: contextvars.ContextVar[
            StateTransaction | None
        ] = contextvars.ContextVar(
            "kivyfn_store_state_transaction",
            default=None,
        )

        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

        super().__init__(**kwargs)

    # -------------------------------------------------
    # Public API
    # -------------------------------------------------

    @overload
    def create(
        self,
        source: type[SingleSchemaT],
    ) -> SingleSchemaT:
        ...

    @overload
    def create(
        self,
        source: type[ManySchemaT],
    ) -> CollectionState[ManySchemaT]:
        ...

    @overload
    def create(
        self,
        source: Query[ManySchemaT],
    ) -> CollectionState[ManySchemaT]:
        ...

    def create(
        self,
        source: (
            type[SingleSchema]
            | type[ManySchema]
            | Query[Any]
        ),
    ) -> Any:
        """Create or return a live state representation."""
        if self._closed:
            raise StoreStateError(
                "The state manager is closed"
            )

        if isinstance(source, Query):
            return self._create_query_state(
                source,
            )

        if not isinstance(source, type):
            raise TypeError(
                (
                    "state.create() requires a SingleSchema class, "
                    "ManySchema class, or Query"
                )
            )

        if issubclass(source, SingleSchema):
            return self._create_single_state(
                source,
            )

        if issubclass(source, ManySchema):
            return self._create_many_state(
                source,
            )

        raise TypeError(
            (
                "state.create() requires a SingleSchema class, "
                "ManySchema class, or Query"
            )
        )

    def transaction(self) -> StateTransaction:
        if self._closed:
            raise StoreStateError(
                "The state manager is closed"
            )

        return StateTransaction(
            self,
        )

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True

        for view in tuple(self._views):
            view.dispose()

        for binding in tuple(
            self._signal_bindings.values()
        ):
            self._dispose_signal_binding(
                binding,
            )

        self._signal_bindings.clear()

        for task in tuple(self._tasks):
            task.cancel()

        self._tasks.clear()
        self._direct_views.clear()
        self.synced = False

    # -------------------------------------------------
    # State creation
    # -------------------------------------------------

    def _create_single_state(
        self,
        schema_type: type[SingleSchemaT],
    ) -> SingleSchemaT:
        self._register_schema_type(
            schema_type,
        )

        schema = self._schema(
            schema_type,
        )

        existing = self._direct_views.get(
            schema.name,
        )

        if (
            existing is not None
            and not existing.disposed
        ):
            if not isinstance(
                existing,
                SingleStateView,
            ):
                raise StoreStateError(
                    (
                        f"State view type mismatch for "
                        f"{schema.name}"
                    )
                )

            return cast(
                SingleSchemaT,
                existing.entity,
            )

        entity = self._entity(
            schema,
            None,
        )

        view = SingleStateView(
            manager=self,
            schema=schema,
            entity=entity,
        )

        self._views.add(
            view,
        )

        self._direct_views[
            schema.name
        ] = view

        self._start_view(
            view,
        )

        return cast(
            SingleSchemaT,
            entity,
        )

    def _create_many_state(
        self,
        schema_type: type[ManySchemaT],
    ) -> CollectionState[ManySchemaT]:
        self._register_schema_type(
            schema_type,
        )

        schema = self._schema(
            schema_type,
        )

        existing = self._direct_views.get(
            schema.name,
        )

        if (
            existing is not None
            and not existing.disposed
        ):
            if not isinstance(
                existing,
                CollectionState,
            ):
                raise StoreStateError(
                    (
                        f"State view type mismatch for "
                        f"{schema.name}"
                    )
                )

            return cast(
                CollectionState[ManySchemaT],
                existing,
            )

        query = Query(
            schema_type,
        )

        collection = CollectionState(
            manager=self,
            schema=schema,
            query=query,
            mutable=True,
        )

        self._views.add(
            collection,
        )

        self._direct_views[
            schema.name
        ] = collection

        self._start_view(
            collection,
        )

        return collection

    def _create_query_state(
        self,
        query: Query[ManySchemaT],
    ) -> CollectionState[ManySchemaT]:
        if any(
            join.alias is not None
            for join in query.joins
        ):
            raise ValueError(
                (
                    "Live state joins use schema relationship "
                    "names and do not support aliases"
                )
            )

        schema_type = self._query_schema_type(
            query,
        )

        self._register_schema_type(
            schema_type,
        )

        schema = self._schema(
            schema_type,
        )

        collection: CollectionState[ManySchemaT] = CollectionState(
            manager=self,
            schema=schema,
            query=query,
            mutable=False,
        )

        self._views.add(
            collection,
        )

        self._start_view(
            collection,
        )

        return collection

    # -------------------------------------------------
    # Query source resolution
    # -------------------------------------------------

    def _query_schema_type(
        self,
        query: Query[ManySchemaT],
    ) -> type[ManySchemaT]:
        source = query.source

        if (
            isinstance(source, type)
            and issubclass(source, ManySchema)
        ):
            return cast(
                type[ManySchemaT],
                source,
            )

        if isinstance(source, str):
            schema_type = self._find_schema_type(
                source,
            )
        elif isinstance(source, Address):
            schema_type = self._schema_type_for_address(
                source,
            )
        else:
            raise TypeError(
                "Query source must reference a ManySchema"
            )

        if not issubclass(
            schema_type,
            ManySchema,
        ):
            raise TypeError(
                "Query source must reference a ManySchema"
            )

        return cast(
            type[ManySchemaT],
            schema_type,
        )

    # -------------------------------------------------
    # Schema/entity management
    # -------------------------------------------------

    def _schema(
        self,
        schema_type: type[SchemaT],
    ) -> SchemaT:
        name = schema_type.__name__

        schema = self._schemas.get(
            name,
        )

        if schema is None:
            schema = schema_type()

            self._schemas[
                name
            ] = schema

            self._schema_types[
                name
            ] = schema_type

        return cast(
            SchemaT,
            schema,
        )

    def _entity(
        self,
        schema: SchemaT,
        row_id: str | None,
    ) -> EntityState[SchemaT]:
        key = (
            schema.name,
            row_id,
        )

        entity = self._entities.get(
            key,
        )

        if entity is not None:
            return cast(
                EntityState[SchemaT],
                entity,
            )

        schema_type = self._schema_types.get(
            schema.name,
        )

        if schema_type is None:
            schema_type = self._find_schema_type(
                schema.name,
            )

            self._register_schema_type(
                schema_type,
            )

        state_type = entity_type(
            cast(
                type[SchemaT],
                schema_type,
            )
        )

        entity = state_type(
            manager=self,
            schema=schema,
            row_id=row_id,
        )

        address = self._addresses.get(
            schema.name,
        )

        if address is not None:
            entity._address = address

        self._entities[
            key
        ] = cast(
            EntityState[Any],
            entity,
        )

        return entity

    # -------------------------------------------------
    # Hydration
    # -------------------------------------------------

    def _hydrate_entity(
        self,
        entity: EntityState[Any],
        values: dict[str, Any],
        query: Query[Any],
        visited: set[
            tuple[str, str | None]
        ]
        | None = None,
    ) -> None:
        seen = (
            visited
            if visited is not None
            else set()
        )

        key = (
            entity.schema.name,
            entity.row_id,
        )

        if key in seen:
            return

        seen.add(
            key,
        )

        entity._hydrate(
            values,
        )

        schema = entity.schema

        for (
            field_name,
            field,
        ) in schema.foreign_key_fields().items():
            relation = field.relation_name(
                field_name,
            )

            if (
                relation == field_name
                or relation not in values
            ):
                continue

            joined = values.get(
                relation,
            )

            foreign_id = values.get(
                field_name,
            )

            if (
                joined is None
                or foreign_id is None
            ):
                entity._set_relation(
                    relation,
                    None,
                )

                continue

            if not isinstance(
                joined,
                Mapping,
            ):
                continue

            schema_type = self._find_schema_type(
                field.schema,
            )

            target_schema = self._schema(
                schema_type,
            )

            self._observe_schema(
                target_schema,
            )

            target = self._entity(
                target_schema,
                str(foreign_id),
            )

            self._hydrate_entity(
                target,
                dict(joined),
                query,
                seen,
            )

            entity._set_relation(
                relation,
                target,
            )

        seen.discard(
            key,
        )

    # -------------------------------------------------
    # Query changes
    # -------------------------------------------------

    def _apply_query_change(
        self,
        view: CollectionState[Any],
        change: QueryChangeSet,
    ) -> None:
        if view.disposed:
            return

        for event in change.events:
            if isinstance(
                event,
                QueryInsertEvent,
            ):
                entity = self._entity(
                    view.schema,
                    event.id,
                )

                self._hydrate_entity(
                    entity,
                    dict(event.new_value),
                    view.query,
                )

                view._insert_entity(
                    entity,
                    notify=True,
                )

                continue

            if isinstance(
                event,
                QueryUpdateEvent,
            ):
                entity = self._entity(
                    view.schema,
                    event.id,
                )

                self._hydrate_entity(
                    entity,
                    dict(event.new_value),
                    view.query,
                )

                if event.id not in view.rows:
                    view._insert_entity(
                        entity,
                        notify=True,
                    )

                continue

            if isinstance(
                event,
                QueryRemoveEvent,
            ):
                view._remove_entity(
                    event.id,
                    notify=True,
                )

    # -------------------------------------------------
    # Synchronization
    # -------------------------------------------------

    def _set_view_synced(
        self,
        view: View,
        value: bool,
    ) -> None:
        if view.disposed:
            self._recalculate_synced()
            return

        view.synced = value

        for entity in self._view_entities.get(
            view,
            (),
        ):
            self._update_entity_synced(
                entity,
            )

        self._recalculate_synced()

    def _register_entity_view(
        self,
        view: View,
        entity: EntityState[Any],
    ) -> None:
        self._view_entities[
            view
        ].add(
            entity,
        )

        self._entity_views[
            entity
        ].add(
            view,
        )

        self._update_entity_synced(
            entity,
        )

    def _unregister_entity_view(
        self,
        view: View,
        entity: EntityState[Any],
    ) -> None:
        self._view_entities.get(
            view,
            set(),
        ).discard(
            entity,
        )

        views = self._entity_views.get(
            entity,
        )

        if views is not None:
            views.discard(
                view,
            )

            if not views:
                self._entity_views.pop(
                    entity,
                    None,
                )

        self._update_entity_synced(
            entity,
        )

    def _update_entity_synced(
        self,
        entity: EntityState[Any],
    ) -> None:
        views = self._entity_views.get(
            entity,
            (),
        )

        entity.synced = any(
            view.synced
            for view in views
        )

    def _recalculate_synced(self) -> None:
        active = [
            view
            for view in self._views
            if not view.disposed
        ]

        self.synced = (
            bool(active)
            and all(
                view.synced
                for view in active
            )
        )

    # -------------------------------------------------
    # Update operations
    # -------------------------------------------------

    def _request_update(
        self,
        entity: EntityState[Any],
        values: Mapping[str, Any],
        *,
        optimistic: bool,
        previous: Mapping[str, Any] | None = None,
    ) -> StateOperation:
        checked = dict(
            values,
        )

        old_values = (
            dict(previous)
            if previous is not None
            else {
                name: deepcopy(
                    getattr(
                        entity,
                        name,
                    )
                )
                for name in checked
            }
        )

        transaction = self._active_transaction.get()

        try:
            entity.schema.challenge_fields(
                checked,
            )
        except Exception as exc:
            if optimistic:
                entity._restore_assignment(
                    old_values,
                )

            if transaction is not None:
                self._attach_operation(
                    transaction.operation,
                    entity=entity,
                )

                transaction.fail(
                    exc,
                )

                return transaction.operation

            operation = StateOperation(
                StateOperationKind.UPDATE,
                schema=entity.schema.name,
                row_id=entity.row_id,
                requested=checked,
                previous=old_values,
                optimistic=optimistic,
            )

            self._attach_operation(
                operation,
                entity=entity,
            )

            self._finish_operation(
                operation,
                self._failed_result(
                    exc,
                ),
            )

            return operation

        if transaction is not None:
            operation = transaction.operation

            self._attach_operation(
                operation,
                entity=entity,
            )

            entity._begin_overlay(
                operation,
                checked,
            )

            try:
                transaction.add_update(
                    entity,
                    checked,
                )
            except Exception as exc:
                entity._discard_overlay(
                    operation,
                    set(checked),
                )

                transaction.fail(
                    exc,
                )

            return operation

        operation = StateOperation(
            StateOperationKind.UPDATE,
            schema=entity.schema.name,
            row_id=entity.row_id,
            requested=checked,
            previous=old_values,
            optimistic=optimistic,
        )

        self._attach_operation(
            operation,
            entity=entity,
        )

        if optimistic:
            entity._begin_overlay(
                operation,
                checked,
            )

        self._submit_update(
            operation,
            entity,
            checked,
        )

        return operation

    # -------------------------------------------------
    # Insert operations
    # -------------------------------------------------

    def _request_insert(
        self,
        collection: CollectionState[Any],
        row_id: str,
        values: Mapping[str, Any],
        *,
        optimistic: bool,
    ) -> StateOperation:
        transaction = self._active_transaction.get()

        built = collection.schema.defaults()

        built.update(
            dict(values),
        )

        try:
            collection.schema.challenge_fields(
                built,
            )
        except Exception as exc:
            if transaction is not None:
                self._attach_operation(
                    transaction.operation,
                    collection=collection,
                )

                transaction.fail(
                    exc,
                )

                return transaction.operation

            operation = StateOperation(
                StateOperationKind.INSERT,
                schema=collection.schema.name,
                row_id=row_id,
                requested=built,
                optimistic=optimistic,
            )

            self._attach_operation(
                operation,
                collection=collection,
            )

            self._finish_operation(
                operation,
                self._failed_result(
                    exc,
                ),
            )

            return operation

        entity = self._entity(
            collection.schema,
            row_id,
        )

        if transaction is not None:
            operation = transaction.operation

            self._attach_operation(
                operation,
                entity=entity,
                collection=collection,
            )

            entity._begin_overlay(
                operation,
                built,
            )

            try:
                transaction.add_insert(
                    collection,
                    entity,
                    built,
                )
            except Exception as exc:
                entity._discard_overlay(
                    operation,
                )

                transaction.fail(
                    exc,
                )

                return operation

            collection._optimistic_insertions.add(
                row_id,
            )

            collection._insert_entity(
                entity,
                notify=True,
                force=True,
            )

            return operation

        operation = StateOperation(
            StateOperationKind.INSERT,
            schema=collection.schema.name,
            row_id=row_id,
            requested=built,
            optimistic=optimistic,
        )

        self._attach_operation(
            operation,
            entity=entity,
            collection=collection,
        )

        if optimistic:
            entity._begin_overlay(
                operation,
                built,
            )

            collection._optimistic_insertions.add(
                row_id,
            )

            collection._insert_entity(
                entity,
                notify=True,
                force=True,
            )

        self._submit_insert(
            operation,
            collection,
            entity,
            built,
        )

        return operation

    # -------------------------------------------------
    # Remove operations
    # -------------------------------------------------

    def _request_remove(
        self,
        collection: CollectionState[Any],
        row_id: str,
        *,
        optimistic: bool,
    ) -> StateOperation:
        entity = cast(
            EntityState[Any],
            collection[row_id],
        )

        transaction = self._active_transaction.get()

        if transaction is not None:
            operation = transaction.operation

            self._attach_operation(
                operation,
                entity=entity,
                collection=collection,
            )

            try:
                cancelled = transaction.add_remove(
                    collection,
                    entity,
                )
            except Exception as exc:
                transaction.fail(
                    exc,
                )

                return operation

            entity._discard_overlay(
                operation,
            )

            if cancelled is StateOperationKind.INSERT:
                collection._optimistic_insertions.discard(
                    row_id,
                )

                collection._remove_entity(
                    row_id,
                    notify=True,
                    force=True,
                )

                return operation

            collection._optimistic_removals[
                row_id
            ] = entity

            collection._remove_entity(
                row_id,
                notify=True,
                force=True,
            )

            return operation

        operation = StateOperation(
            StateOperationKind.REMOVE,
            schema=collection.schema.name,
            row_id=row_id,
            previous={
                name: deepcopy(
                    getattr(
                        entity,
                        name,
                    )
                )
                for name
                in entity.schema.field_names()
            },
            optimistic=optimistic,
        )

        self._attach_operation(
            operation,
            entity=entity,
            collection=collection,
        )

        if optimistic:
            collection._optimistic_removals[
                row_id
            ] = entity

            collection._remove_entity(
                row_id,
                notify=True,
                force=True,
            )

        self._submit_remove(
            operation,
            collection,
            entity,
        )

        return operation

    # -------------------------------------------------
    # Signal operations
    # -------------------------------------------------

    def _request_signal(
        self,
        entity: EntityState[Any],
        signal: str,
        data: Any,
    ) -> StateOperation:
        transaction = self._active_transaction.get()

        try:
            entity.schema.get_signal(
                signal,
            )
        except Exception as exc:
            if transaction is not None:
                self._attach_operation(
                    transaction.operation,
                    entity=entity,
                )

                transaction.fail(
                    exc,
                )

                return transaction.operation

            operation = StateOperation(
                StateOperationKind.SIGNAL,
                schema=entity.schema.name,
                row_id=entity.row_id,
                signal=signal,
                requested=data,
            )

            self._attach_operation(
                operation,
                entity=entity,
            )

            self._finish_operation(
                operation,
                self._failed_result(
                    exc,
                ),
            )

            return operation

        if transaction is not None:
            operation = transaction.operation

            self._attach_operation(
                operation,
                entity=entity,
            )

            transaction.add_signal(
                entity,
                signal,
                data,
            )

            return operation

        operation = StateOperation(
            StateOperationKind.SIGNAL,
            schema=entity.schema.name,
            row_id=entity.row_id,
            signal=signal,
            requested=data,
        )

        self._attach_operation(
            operation,
            entity=entity,
        )

        self._submit_signal(
            operation,
            entity,
            signal,
            data,
        )

        return operation

    # -------------------------------------------------
    # Operation lifecycle
    # -------------------------------------------------

    def _attach_operation(
        self,
        operation: StateOperation,
        *,
        entity: EntityState[Any] | None = None,
        collection: CollectionState[Any] | None = None,
    ) -> None:
        operation._attach(
            self,
        )

        if entity is not None:
            operation._attach(
                entity,
            )

            for view in self._entity_views.get(
                entity,
                (),
            ):
                operation._attach(
                    view,
                )

        if collection is not None:
            operation._attach(
                collection,
            )

    def _complete_update(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        values: dict[str, Any],
        result: StateResult,
    ) -> None:
        if operation.optimistic:
            entity._resolve_overlay(
                operation,
                accepted=result.ok,
                values=values,
            )

        elif result.ok:
            entity._apply_confirmed(
                values,
            )

        self._finish_operation(
            operation,
            result,
        )

    def _complete_insert(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
        values: dict[str, Any],
        result: StateResult,
    ) -> None:
        row_id = cast(
            str,
            operation.row_id,
        )

        if operation.optimistic:
            entity._resolve_overlay(
                operation,
                accepted=result.ok,
                values=values,
            )

            collection._optimistic_insertions.discard(
                row_id,
            )

        if result.ok:
            entity._hydrate(
                values,
            )

            collection._insert_entity(
                entity,
                notify=True,
                force=True,
            )

        elif operation.optimistic:
            collection._remove_entity(
                row_id,
                notify=True,
                force=True,
            )

        self._finish_operation(
            operation,
            result,
        )

    def _complete_remove(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
        result: StateResult,
    ) -> None:
        row_id = cast(
            str,
            operation.row_id,
        )

        if result.ok:
            collection._optimistic_removals.pop(
                row_id,
                None,
            )

            collection._remove_entity(
                row_id,
                notify=True,
                force=True,
            )

        elif operation.optimistic:
            retained = (
                collection._optimistic_removals.pop(
                    row_id,
                    entity,
                )
            )

            collection._insert_entity(
                retained,
                notify=True,
                force=True,
            )

        self._finish_operation(
            operation,
            result,
        )

    def _finish_operation(
        self,
        operation: StateOperation,
        result: StateResult,
    ) -> None:
        operation._finish(
            result,
        )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    def _dispatch_signal(
        self,
        event: SignalEvent[Any],
    ) -> None:
        for entity in tuple(
            self._entities.values()
        ):
            if entity.address != event.address:
                continue

            if entity.row_id != event.id:
                continue

            try:
                entity.dispatch(
                    f"on_{event.signal}",
                    event,
                )
            except Exception:
                logger.exception(
                    (
                        "State signal callback failed: "
                        "schema=%s row_id=%s signal=%s"
                    ),
                    entity.schema.name,
                    entity.row_id,
                    event.signal,
                )

    # -------------------------------------------------
    # Dispatching
    # -------------------------------------------------

    def _dispatch(
        self,
        callback: Callable[..., Any],
        *args: Any,
    ) -> None:
        if self._dispatcher is None:
            callback(
                *args,
            )

            return

        self._dispatcher(
            callback,
            *args,
        )

    # -------------------------------------------------
    # Tasks
    # -------------------------------------------------

    def _track_task(
        self,
        coroutine: Any,
    ) -> asyncio.Task[Any]:
        try:
            task = asyncio.create_task(
                coroutine,
            )
        except RuntimeError:
            close = getattr(
                coroutine,
                "close",
                None,
            )

            if callable(close):
                close()

            raise StoreStateError(
                (
                    "Remote state requires a running "
                    "asyncio event loop"
                )
            ) from None

        self._tasks.add(
            task,
        )

        task.add_done_callback(
            self._task_done,
        )

        return task

    def _task_done(
        self,
        task: asyncio.Task[Any],
    ) -> None:
        self._tasks.discard(
            task,
        )

        if task.cancelled():
            return

        error = task.exception()

        if error is not None:
            logger.error(
                "State background task failed: %s",
                error,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
            )

    # -------------------------------------------------
    # Schema discovery
    # -------------------------------------------------

    def _register_schema_type(
        self,
        schema_type: type[Schema],
    ) -> None:
        self._schema_types[
            schema_type.__name__
        ] = schema_type

    def _find_schema_type(
        self,
        name: str,
    ) -> type[Schema]:
        known = self._schema_types.get(
            name,
        )

        if known is not None:
            return known

        for schema_type in _schema_subclasses(
            Schema,
        ):
            if schema_type.__name__ == name:
                self._register_schema_type(
                    schema_type,
                )

                return schema_type

        raise StoreStateError(
            (
                f"Schema class {name!r} is not "
                "loaded in this process"
            )
        )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def _enter_transaction(
        self,
        transaction: StateTransaction,
    ) -> Any:
        active = self._active_transaction.get()

        if active is not None:
            raise RuntimeError(
                "Nested state transactions are not supported"
            )

        return self._active_transaction.set(
            transaction,
        )

    def _leave_transaction(
        self,
        token: Any,
    ) -> None:
        self._active_transaction.reset(
            token,
        )

    def _cancel_transaction(
        self,
        transaction: StateTransaction,
        error: BaseException,
    ) -> None:
        self._complete_state_transaction(
            transaction,
            self._failed_result(
                error,
            ),
        )

    def _complete_state_transaction(
        self,
        transaction: StateTransaction,
        result: StateResult,
    ) -> None:
        operation = transaction.operation

        for action in transaction.actions:
            entity = action.entity

            if action.kind is StateOperationKind.UPDATE:
                entity._resolve_overlay(
                    operation,
                    accepted=result.ok,
                    values=action.values,
                )

                continue

            if action.kind is StateOperationKind.SIGNAL:
                continue

            collection = action.collection

            if collection is None:
                continue

            row_id = cast(
                str,
                entity.row_id,
            )

            if action.kind is StateOperationKind.INSERT:
                entity._resolve_overlay(
                    operation,
                    accepted=result.ok,
                    values=action.values,
                )

                collection._optimistic_insertions.discard(
                    row_id,
                )

                if result.ok:
                    entity._hydrate(
                        action.values or {},
                    )

                    collection._insert_entity(
                        entity,
                        notify=True,
                        force=True,
                    )

                else:
                    collection._remove_entity(
                        row_id,
                        notify=True,
                        force=True,
                    )

                continue

            if result.ok:
                collection._optimistic_removals.pop(
                    row_id,
                    None,
                )

                collection._remove_entity(
                    row_id,
                    notify=True,
                    force=True,
                )

            else:
                retained = (
                    collection._optimistic_removals.pop(
                        row_id,
                        entity,
                    )
                )

                collection._insert_entity(
                    retained,
                    notify=True,
                    force=True,
                )

        self._finish_operation(
            operation,
            result,
        )

    # -------------------------------------------------
    # Address/schema observation
    # -------------------------------------------------

    def _observe_schema(
        self,
        schema: Schema,
    ) -> None:
        """Prepare address and signal support for joined schemas."""

    def _update_address(
        self,
        schema_name: str,
        address: Address[Any],
    ) -> None:
        self._addresses[
            schema_name
        ] = address

        for (
            name,
            _row_id,
        ), entity in tuple(
            self._entities.items()
        ):
            if name == schema_name:
                entity._address = address

    # -------------------------------------------------
    # Disposal
    # -------------------------------------------------

    def _dispose_view(
        self,
        view: View,
    ) -> None:
        self._views.discard(
            view,
        )

        if (
            self._direct_views.get(
                view.schema.name,
            )
            is view
        ):
            self._direct_views.pop(
                view.schema.name,
                None,
            )

        for entity in tuple(
            self._view_entities.pop(
                view,
                (),
            )
        ):
            self._unregister_entity_view(
                view,
                entity,
            )

        self._dispose_subscription(
            view._subscription,
        )

        self._recalculate_synced()

    # -------------------------------------------------
    # Errors
    # -------------------------------------------------

    @staticmethod
    def _failed_result(
        error: BaseException,
    ) -> StateResult:
        public_error: Any = error

        if isinstance(
            error,
            StoreOperationError,
        ):
            public_error = error.failure()

        return StateResult(
            ok=False,
            changed=False,
            error=public_error,
        )

    # -------------------------------------------------
    # Backend hooks
    # -------------------------------------------------

    @abstractmethod
    def _start_view(
        self,
        view: View,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def _schema_type_for_address(
        self,
        address: Address[Any],
    ) -> type[Schema]:
        raise NotImplementedError

    @abstractmethod
    def _submit_update(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        values: dict[str, Any],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def _submit_insert(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
        values: dict[str, Any],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def _submit_remove(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def _submit_signal(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        signal: str,
        data: Any,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def _submit_state_transaction(
        self,
        transaction: StateTransaction,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def _dispose_subscription(
        self,
        subscription: Any,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def _dispose_signal_binding(
        self,
        binding: Any,
    ) -> None:
        raise NotImplementedError


def _schema_subclasses(
    schema_type: type[Schema],
) -> tuple[type[Schema], ...]:
    result: list[type[Schema]] = []

    for child in schema_type.__subclasses__():
        result.append(
            child,
        )

        result.extend(
            _schema_subclasses(
                child,
            )
        )

    return tuple(
        result,
    )
