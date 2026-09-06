from __future__ import annotations

from typing import Any

from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import SetEvent
from kivyfn.store.local.store import LocalStore
from kivyfn.store.query import QueryChangeSet
from kivyfn.store.schema import ManySchema, Schema
from kivyfn.store.state.collection import CollectionState
from kivyfn.store.state.entity import EntityState
from kivyfn.store.state.manager import BaseStateManager, View
from kivyfn.store.state.operation import (
    StateOperation,
    StateOperationKind,
    StateResult,
)
from kivyfn.store.state.transaction import StateTransaction
from kivyfn.store.state.view import SingleStateView
from kivyfn.store.transaction import Transaction, TransactionResult


class LocalStateManager(BaseStateManager):
    """Live state backed by a synchronous LocalStore."""

    def __init__(
        self,
        store: LocalStore,
        **kwargs: Any,
    ) -> None:
        self._store = store

        super().__init__(**kwargs)

        self._load_catalog()
        self.connected = True

    # -------------------------------------------------
    # Catalog
    # -------------------------------------------------

    def _load_catalog(self) -> None:
        metadata = self._store.engine.metadata

        for schema_name, node in metadata.nodes_by_schema.items():
            schema_type = type(node.schema)

            self._schemas[schema_name] = node.schema

            self._register_schema_type(
                schema_type,
            )

            self._update_address(
                schema_name,
                node.address,
            )

    # -------------------------------------------------
    # Views
    # -------------------------------------------------

    def _start_view(
        self,
        view: View,
    ) -> None:
        if isinstance(view, SingleStateView):
            self._start_single_view(
                view,
            )

            return

        self._start_collection_view(
            view,
        )

    def _start_single_view(
        self,
        view: SingleStateView[Any],
    ) -> None:
        node = self._store.engine.metadata.get_node_by_schema(
            view.schema.name,
        )

        self._update_address(
            view.schema.name,
            node.address,
        )

        view._address = node.address

        view._subscription = self._store.subscription.set(
            lambda event: self._on_single_change(
                view,
                event,
            ),
            node.address,
        )

        response = self._store.snapshot(
            node.address,
        )

        if not response.ok:
            view._subscription.dispose()
            view._subscription = None

            raise StoreStateError(
                (
                    f"Could not load state for "
                    f"{view.schema.name}: {response.error}"
                )
            )

        if not isinstance(response.data, dict):
            view._subscription.dispose()
            view._subscription = None

            raise StoreStateError(
                (
                    f"Single state snapshot for "
                    f"{view.schema.name} is invalid"
                )
            )

        view._replace_snapshot(
            response.data,
        )

        self._set_view_synced(
            view,
            True,
        )

        self._ensure_signal_bindings(
            view.schema,
            node.address,
        )

    def _start_collection_view(
        self,
        view: CollectionState[Any],
    ) -> None:
        node = self._store.engine.metadata.get_node_by_schema(
            view.schema.name,
        )

        self._update_address(
            view.schema.name,
            node.address,
        )

        view._address = node.address

        view._subscription = self._store.subscribe(
            view.query,
            lambda change: self._on_query_change(
                view,
                change,
            ),
        )

        view._replace_snapshot(
            view._subscription.data,
            notify=False,
        )

        self._set_view_synced(
            view,
            True,
        )

        self._ensure_signal_bindings(
            view.schema,
            node.address,
        )

    def _on_single_change(
        self,
        view: SingleStateView[Any],
        event: SetEvent,
    ) -> None:
        view._replace_snapshot(
            {
                event.key: event.new_value,
            }
        )

    def _on_query_change(
        self,
        view: CollectionState[Any],
        change: QueryChangeSet,
    ) -> None:
        self._apply_query_change(
            view,
            change,
        )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    def _ensure_signal_bindings(
        self,
        schema: Schema,
        address: Address[Any],
    ) -> None:
        for signal_name in schema.signal_names():
            key = (
                schema.name,
                signal_name,
            )

            if key in self._signal_bindings:
                continue

            binding = self._store.signal.bind(
                address,
                signal_name,
                self._dispatch_signal,
            )

            self._signal_bindings[
                key
            ] = binding

    def _observe_schema(
        self,
        schema: Schema,
    ) -> None:
        node = self._store.engine.metadata.get_node_by_schema(
            schema.name,
        )

        self._update_address(
            schema.name,
            node.address,
        )

        self._ensure_signal_bindings(
            schema,
            node.address,
        )

    # -------------------------------------------------
    # Schema resolution
    # -------------------------------------------------

    def _schema_type_for_address(
        self,
        address: Address[Any],
    ) -> type[Schema]:
        node = self._store.engine.metadata.get_node_by_address(
            address,
        )

        schema_type = type(
            node.schema,
        )

        self._register_schema_type(
            schema_type,
        )

        return schema_type

    # -------------------------------------------------
    # Updates
    # -------------------------------------------------

    def _submit_update(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        values: dict[str, Any],
    ) -> None:
        transaction = Transaction()
        address = self._require_entity_address(
            entity,
        )

        for field_name, value in values.items():
            if isinstance(
                entity.schema,
                ManySchema,
            ):
                transaction.setrow(
                    address,
                    entity.row_id,
                    field_name,
                    value,
                )

                continue

            transaction.set(
                address,
                field_name,
                value,
            )

        result = self._commit(
            transaction,
        )

        self._complete_update(
            operation,
            entity,
            values,
            result,
        )

    # -------------------------------------------------
    # Inserts
    # -------------------------------------------------

    def _submit_insert(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
        values: dict[str, Any],
    ) -> None:
        transaction = Transaction().insert(
            self._require_collection_address(
                collection,
            ),
            values,
            id=entity.row_id,
        )

        result = self._commit(
            transaction,
        )

        self._complete_insert(
            operation,
            collection,
            entity,
            values,
            result,
        )

    # -------------------------------------------------
    # Removes
    # -------------------------------------------------

    def _submit_remove(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
    ) -> None:
        row_id = entity.row_id

        if row_id is None:
            raise ValueError(
                "Collection rows require an ID"
            )

        transaction = Transaction().remove(
            self._require_collection_address(
                collection,
            ),
            row_id,
        )

        result = self._commit(
            transaction,
        )

        self._complete_remove(
            operation,
            collection,
            entity,
            result,
        )

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    def _submit_signal(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        signal: str,
        data: Any,
    ) -> None:
        try:
            event = self._store.signal.emit(
                self._require_entity_address(
                    entity,
                ),
                signal,
                data,
                id=entity.row_id,
            )

            result = StateResult(
                ok=True,
                changed=True,
                data=event,
            )

        except Exception as exc:
            result = self._failed_result(
                exc,
            )

        self._finish_operation(
            operation,
            result,
        )

    # -------------------------------------------------
    # Transactions
    # -------------------------------------------------

    def _submit_state_transaction(
        self,
        transaction: StateTransaction,
    ) -> None:
        self._attach_operation(
            transaction.operation,
        )

        commands = Transaction()
        signal_actions = []

        try:
            for action in transaction.actions:
                if action.kind is StateOperationKind.SIGNAL:
                    signal_actions.append(
                        action,
                    )

                    continue

                entity = action.entity

                address = self._require_entity_address(
                    entity,
                )

                if action.kind is StateOperationKind.UPDATE:
                    for field_name, value in (
                        action.values or {}
                    ).items():
                        if isinstance(
                            entity.schema,
                            ManySchema,
                        ):
                            commands.setrow(
                                address,
                                entity.row_id,
                                field_name,
                                value,
                            )

                            continue

                        commands.set(
                            address,
                            field_name,
                            value,
                        )

                    continue

                if action.kind is StateOperationKind.INSERT:
                    commands.insert(
                        address,
                        action.values or {},
                        id=entity.row_id,
                    )

                    continue

                row_id = entity.row_id

                if row_id is None:
                    raise ValueError(
                        "Collection rows require an ID"
                    )

                commands.remove(
                    address,
                    row_id,
                )

            for action in signal_actions:
                signal = action.signal

                if signal is None:
                    continue

                self._store.signal.validate(
                    self._require_entity_address(
                        action.entity,
                    ),
                    signal,
                    id=action.entity.row_id,
                )

            result = (
                self._commit(commands)
                if commands
                else StateResult(
                    ok=True,
                    changed=False,
                )
            )

            if result.ok:
                events = self._emit_transaction_signals(
                    signal_actions,
                )

                if events:
                    result = StateResult(
                        ok=True,
                        changed=True,
                        data={
                            "transaction": result.data,
                            "signals": events,
                        },
                    )

        except Exception as exc:
            result = self._failed_result(
                exc,
            )

        self._complete_state_transaction(
            transaction,
            result,
        )

    def _emit_transaction_signals(
        self,
        actions: list[Any],
    ) -> list[Any]:
        events = []

        for action in actions:
            signal = action.signal

            if signal is None:
                continue

            events.append(
                self._store.signal.emit(
                    self._require_entity_address(
                        action.entity,
                    ),
                    signal,
                    action.data,
                    id=action.entity.row_id,
                )
            )

        return events

    # -------------------------------------------------
    # Commit
    # -------------------------------------------------

    def _commit(
        self,
        transaction: Transaction,
    ) -> StateResult:
        try:
            response = self._store.commit(
                transaction,
            )

        except Exception as exc:
            return self._failed_result(
                exc,
            )

        if response.ok is False:
            return StateResult(
                ok=False,
                changed=False,
                error=response.error,
            )

        data = response.data

        return StateResult(
            ok=True,
            changed=_transaction_changed(
                data,
            ),
            data=data,
        )

    # -------------------------------------------------
    # Disposal
    # -------------------------------------------------

    def _dispose_subscription(
        self,
        subscription: Any,
    ) -> None:
        if subscription is not None:
            subscription.dispose()

    def _dispose_signal_binding(
        self,
        binding: Any,
    ) -> None:
        if binding is not None:
            binding.dispose()

    # -------------------------------------------------
    # Address helpers
    # -------------------------------------------------

    @staticmethod
    def _require_entity_address(
        entity: EntityState[Any],
    ) -> Address[Any]:
        address = entity.address

        if address is None:
            raise RuntimeError(
                "State entity has no resolved address"
            )

        return address

    @staticmethod
    def _require_collection_address(
        collection: CollectionState[Any],
    ) -> Address[Any]:
        address = collection.address

        if address is None:
            raise RuntimeError(
                "State collection has no resolved address"
            )

        return address

    # -------------------------------------------------
    # Properties
    # -------------------------------------------------

    @property
    def store(self) -> LocalStore:
        return self._store


def _transaction_changed(
    result: TransactionResult,
) -> bool:
    return bool(
        result.inserted
        or result.removed
        or result.updated
    )
