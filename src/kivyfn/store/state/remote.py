from __future__ import annotations

import asyncio

from collections.abc import Mapping
from typing import Any

from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import SetEvent
from kivyfn.store.query import QueryChangeSet
from kivyfn.store.remote.state import ConnectionState
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.remote.sync import SyncSnapshot
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


class RemoteStateManager(BaseStateManager):
    """Live state backed by a reconnecting RemoteStore."""

    def __init__(
        self,
        store: RemoteStore,
        **kwargs: Any,
    ) -> None:
        self._store = store
        self._address_tasks: dict[
            str,
            asyncio.Task[Address[Any]],
        ] = {}
        self._signal_tasks: dict[
            tuple[str, str],
            asyncio.Task[Any],
        ] = {}
        self._schema_tasks: dict[
            str,
            asyncio.Task[Any],
        ] = {}
        self._single_buffers: dict[
            SingleStateView[Any],
            list[SetEvent],
        ] = {}
        self._single_revisions: dict[
            SingleStateView[Any],
            tuple[str, int],
        ] = {}
        self._mutation_lock = asyncio.Lock()

        super().__init__(**kwargs)

        self.connected = store.connected
        self._store.client.add_state_listener(
            self._on_connection_state,
        )

    def close(self) -> None:
        self._store.client.remove_state_listener(
            self._on_connection_state,
        )
        super().close()

    # -------------------------------------------------
    # Connection state
    # -------------------------------------------------

    def _on_connection_state(
        self,
        state: ConnectionState,
    ) -> None:
        self._dispatch(
            self._apply_connection_state,
            self._store.connected,
            state,
        )

        if state is not ConnectionState.READY:
            return

        for view in tuple(self._views):
            if not isinstance(view, SingleStateView):
                continue

            if view.disposed or view._subscription is None:
                continue

            self._request_single_resync(view)

    def _apply_connection_state(
        self,
        connected: bool,
        state: ConnectionState,
    ) -> None:
        if self._closed:
            return

        self.connected = connected

        if state is ConnectionState.READY:
            return

        for view in tuple(self._views):
            self._set_view_synced(
                view,
                False,
            )

    # -------------------------------------------------
    # Views
    # -------------------------------------------------

    def _start_view(
        self,
        view: View,
    ) -> None:
        self._track_task(
            self._initialize_view(view),
        )

    async def _initialize_view(
        self,
        view: View,
    ) -> None:
        while not self._closed and not view.disposed:
            await self._store.client.wait_until_ready()

            if self._closed or view.disposed:
                return

            try:
                if isinstance(view, SingleStateView):
                    await self._initialize_single_view(view)
                else:
                    await self._initialize_collection_view(view)

                return

            except asyncio.CancelledError:
                raise

            except Exception:
                self._dispatch(
                    self._set_view_synced,
                    view,
                    False,
                )

                if self._store.ready:
                    raise

    async def _initialize_single_view(
        self,
        view: SingleStateView[Any],
    ) -> None:
        address = await self._resolve_address(
            view.schema.name,
        )

        self._single_buffers[view] = []
        subscription: Any = None

        try:
            response = await self._store.subscription.set(
                lambda event: self._on_single_change(
                    view,
                    event,
                ),
                address,
            )

            if response.ok is False:
                raise StoreStateError(
                    "State subscription failed: "
                    f"{response.error}"
                )

            subscription = response.data
            snapshot_response = await self._store.sync_snapshot(
                address,
            )

            if snapshot_response.ok is False:
                raise StoreStateError(
                    "State snapshot failed: "
                    f"{snapshot_response.error}"
                )

            snapshot = snapshot_response.data

            if not isinstance(snapshot.data, Mapping):
                raise StoreStateError(
                    "Single state snapshot is invalid"
                )

            self._dispatch(
                self._install_single_view,
                view,
                address,
                subscription,
                snapshot,
            )

            await self._ensure_signal_bindings(
                view.schema,
                address,
            )

        except BaseException:
            self._single_buffers.pop(
                view,
                None,
            )

            if subscription is not None:
                await subscription.dispose()

            raise

    async def _initialize_collection_view(
        self,
        view: CollectionState[Any],
    ) -> None:
        address = await self._resolve_address(
            view.schema.name,
        )

        response = await self._store.subscribe(
            view.query,
            lambda change: self._on_query_change(
                view,
                change,
            ),
            sync_callback=(
                lambda value, reason: self._on_query_sync(
                    view,
                    value,
                    reason,
                )
            ),
        )

        if response.ok is False:
            raise StoreStateError(
                "State query subscription failed: "
                f"{response.error}"
            )

        subscription = response.data

        self._dispatch(
            self._install_collection_view,
            view,
            address,
            subscription,
        )

        await self._ensure_signal_bindings(
            view.schema,
            address,
        )

    def _install_single_view(
        self,
        view: SingleStateView[Any],
        address: Address[Any],
        subscription: Any,
        snapshot: SyncSnapshot[Any],
    ) -> None:
        if view.disposed:
            self._dispose_subscription(
                subscription,
            )
            self._single_buffers.pop(
                view,
                None,
            )
            return

        self._update_address(
            view.schema.name,
            address,
        )
        view._address = address
        view._subscription = subscription

        self._apply_single_snapshot(
            view,
            snapshot,
        )

    def _install_collection_view(
        self,
        view: CollectionState[Any],
        address: Address[Any],
        subscription: Any,
    ) -> None:
        if view.disposed:
            self._dispose_subscription(
                subscription,
            )
            return

        self._update_address(
            view.schema.name,
            address,
        )
        view._address = address
        view._subscription = subscription

        view._replace_snapshot(
            subscription.data,
            notify=False,
        )

        self._set_view_synced(
            view,
            subscription.synchronized,
        )

    # -------------------------------------------------
    # Single-state synchronization
    # -------------------------------------------------

    def _on_single_change(
        self,
        view: SingleStateView[Any],
        event: SetEvent,
    ) -> None:
        self._dispatch(
            self._apply_single_change,
            view,
            event,
        )

    def _apply_single_change(
        self,
        view: SingleStateView[Any],
        event: SetEvent,
    ) -> None:
        if view.disposed:
            return

        buffer = self._single_buffers.get(view)

        if buffer is not None:
            buffer.append(event)
            return

        instance_id = event.server_instance_id
        revision = event.revision
        current = self._single_revisions.get(view)

        if (
            instance_id is None
            or revision is None
            or current is None
        ):
            view._replace_snapshot(
                {
                    event.key: event.new_value,
                }
            )
            self._set_view_synced(
                view,
                False,
            )
            self._request_single_resync(view)
            return

        current_instance, current_revision = current

        if instance_id != current_instance:
            self._set_view_synced(
                view,
                False,
            )
            self._request_single_resync(view)
            return

        if revision <= current_revision:
            return

        if revision != current_revision + 1:
            self._set_view_synced(
                view,
                False,
            )
            self._request_single_resync(view)
            return

        view._replace_snapshot(
            {
                event.key: event.new_value,
            }
        )
        self._single_revisions[view] = (
            instance_id,
            revision,
        )

    def _request_single_resync(
        self,
        view: SingleStateView[Any],
    ) -> None:
        if self._closed or view.disposed:
            return

        if view in self._single_buffers:
            return

        self._single_buffers[view] = []

        self._track_task(
            self._resync_single_view(view),
        )

    async def _resync_single_view(
        self,
        view: SingleStateView[Any],
    ) -> None:
        try:
            await self._store.client.wait_until_ready()

            if self._closed or view.disposed:
                return

            address = view.address

            if address is None:
                address = await self._resolve_address(
                    view.schema.name,
                )

            response = await self._store.sync_snapshot(
                address,
            )

            if response.ok is False:
                raise StoreStateError(
                    "State resynchronization failed: "
                    f"{response.error}"
                )

            snapshot = response.data

            if not isinstance(snapshot.data, Mapping):
                raise StoreStateError(
                    "Single state snapshot is invalid"
                )

            self._dispatch(
                self._apply_single_snapshot,
                view,
                snapshot,
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            self._dispatch(
                self._single_resync_failed,
                view,
            )
            raise

    def _apply_single_snapshot(
        self,
        view: SingleStateView[Any],
        snapshot: SyncSnapshot[Any],
    ) -> None:
        if view.disposed:
            self._single_buffers.pop(
                view,
                None,
            )
            return

        data = snapshot.data

        if not isinstance(data, Mapping):
            self._single_resync_failed(view)
            return

        view._replace_snapshot(
            dict(data),
        )

        instance_id = snapshot.server_instance_id
        revision = snapshot.revision
        buffer = self._single_buffers.pop(
            view,
            [],
        )

        valid = True

        for event in buffer:
            event_instance = event.server_instance_id
            event_revision = event.revision

            if (
                event_instance != instance_id
                or event_revision is None
            ):
                valid = False
                break

            if event_revision <= revision:
                continue

            if event_revision != revision + 1:
                valid = False
                break

            view._replace_snapshot(
                {
                    event.key: event.new_value,
                }
            )
            revision = event_revision

        self._single_revisions[view] = (
            instance_id,
            revision,
        )

        self._set_view_synced(
            view,
            valid,
        )

    def _single_resync_failed(
        self,
        view: SingleStateView[Any],
    ) -> None:
        self._single_buffers.pop(
            view,
            None,
        )
        self._set_view_synced(
            view,
            False,
        )

    # -------------------------------------------------
    # Query synchronization
    # -------------------------------------------------

    def _on_query_change(
        self,
        view: CollectionState[Any],
        change: QueryChangeSet,
    ) -> None:
        self._dispatch(
            self._apply_query_change,
            view,
            change,
        )

    def _on_query_sync(
        self,
        view: CollectionState[Any],
        synchronized: bool,
        reason: str,
    ) -> None:
        self._dispatch(
            self._set_view_synced,
            view,
            synchronized,
        )

    # -------------------------------------------------
    # Schema/address discovery
    # -------------------------------------------------

    async def _resolve_address(
        self,
        schema_name: str,
    ) -> Address[Any]:
        address = self._addresses.get(
            schema_name,
        )

        if address is not None:
            return address

        task = self._address_tasks.get(
            schema_name,
        )

        if task is None:
            task = self._track_task(
                self._load_address(schema_name),
            )
            task.set_name(
                f"state-schema-{schema_name}"
            )
            self._address_tasks[
                schema_name
            ] = task

        try:
            return await task
        finally:
            if task.done():
                self._address_tasks.pop(
                    schema_name,
                    None,
                )

    async def _load_address(
        self,
        schema_name: str,
    ) -> Address[Any]:
        response = await self._store.schema_info(
            schema_name,
        )

        if response.ok is False:
            raise StoreStateError(
                f"Could not resolve schema {schema_name!r}: "
                f"{response.error}"
            )

        raw_address = response.data.get(
            "address"
        )

        if not isinstance(raw_address, list) or not all(
            isinstance(part, str)
            for part in raw_address
        ):
            raise StoreStateError(
                (
                    "Server returned an invalid address for "
                    f"{schema_name!r}"
                )
            )

        address = Address(
            *raw_address,
        )

        self._dispatch(
            self._update_address,
            schema_name,
            address,
        )

        return address

    def _observe_schema(
        self,
        schema: Schema,
    ) -> None:
        if self._closed:
            return

        task = self._schema_tasks.get(
            schema.name,
        )

        if task is not None and not task.done():
            return

        task = self._track_task(
            self._prepare_schema(schema),
        )
        task.set_name(
            f"state-observe-{schema.name}"
        )
        self._schema_tasks[
            schema.name
        ] = task
        task.add_done_callback(
            lambda current, name=schema.name: (
                self._schema_tasks.pop(
                    name,
                    None,
                )
            )
        )

    async def _prepare_schema(
        self,
        schema: Schema,
    ) -> None:
        await self._store.client.wait_until_ready()

        if self._closed:
            return

        address = await self._resolve_address(
            schema.name,
        )

        await self._ensure_signal_bindings(
            schema,
            address,
        )

    async def _ensure_signal_bindings(
        self,
        schema: Schema,
        address: Address[Any],
    ) -> None:
        pending: list[asyncio.Task[Any]] = []

        for signal_name in schema.signal_names():
            key = (
                schema.name,
                signal_name,
            )

            if key in self._signal_bindings:
                continue

            task = self._signal_tasks.get(
                key,
            )

            if task is None:
                task = self._track_task(
                    self._bind_signal(
                        key,
                        address,
                        signal_name,
                    )
                )
                task.set_name(
                    (
                        f"state-signal-{schema.name}-"
                        f"{signal_name}"
                    )
                )
                self._signal_tasks[
                    key
                ] = task

            pending.append(
                task,
            )

        if pending:
            await asyncio.gather(
                *pending,
            )

    async def _bind_signal(
        self,
        key: tuple[str, str],
        address: Address[Any],
        signal_name: str,
    ) -> None:
        try:
            response = await self._store.signal.bind(
                address,
                signal_name,
                lambda event: self._dispatch(
                    self._dispatch_signal,
                    event,
                ),
            )

            if response.ok is False:
                raise StoreStateError(
                    "State signal binding failed: "
                    f"{response.error}"
                )

            self._signal_bindings[
                key
            ] = response.data

        finally:
            self._signal_tasks.pop(
                key,
                None,
            )

    def _schema_type_for_address(
        self,
        address: Address[Any],
    ) -> type[Schema]:
        for name, known in self._addresses.items():
            if known == address:
                return self._find_schema_type(
                    name,
                )

        raise TypeError(
            (
                "Remote state queries using an Address require "
                "that schema to have already been created"
            )
        )

    # -------------------------------------------------
    # Updates
    # -------------------------------------------------

    def _submit_update(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        values: dict[str, Any],
    ) -> None:
        if not self._store.ready:
            self._complete_update(
                operation,
                entity,
                values,
                _offline_result(),
            )
            return

        self._track_task(
            self._run_mutation(
                self._update_async,
                operation,
                entity,
                values,
            )
        )

    async def _run_mutation(
        self,
        callback: Any,
        *args: Any,
    ) -> None:
        async with self._mutation_lock:
            await callback(
                *args,
            )

    async def _update_async(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        values: dict[str, Any],
    ) -> None:
        try:
            address = await self._entity_address(
                entity,
            )
            transaction = Transaction()

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
                else:
                    transaction.set(
                        address,
                        field_name,
                        value,
                    )

            result = await self._commit(
                transaction,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            result = self._failed_result(
                exc,
            )

        self._dispatch(
            self._complete_update,
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
        if not self._store.ready:
            self._complete_insert(
                operation,
                collection,
                entity,
                values,
                _offline_result(),
            )
            return

        self._track_task(
            self._run_mutation(
                self._insert_async,
                operation,
                collection,
                entity,
                values,
            )
        )

    async def _insert_async(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
        values: dict[str, Any],
    ) -> None:
        try:
            address = await self._entity_address(
                entity,
            )

            transaction = Transaction().insert(
                address,
                values,
                id=entity.row_id,
            )

            result = await self._commit(
                transaction,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            result = self._failed_result(
                exc,
            )

        self._dispatch(
            self._complete_insert,
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
        if not self._store.ready:
            self._complete_remove(
                operation,
                collection,
                entity,
                _offline_result(),
            )
            return

        self._track_task(
            self._run_mutation(
                self._remove_async,
                operation,
                collection,
                entity,
            )
        )

    async def _remove_async(
        self,
        operation: StateOperation,
        collection: CollectionState[Any],
        entity: EntityState[Any],
    ) -> None:
        try:
            address = await self._entity_address(
                entity,
            )
            row_id = entity.row_id

            if row_id is None:
                raise ValueError(
                    "Collection rows require an ID"
                )

            result = await self._commit(
                Transaction().remove(
                    address,
                    row_id,
                )
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            result = self._failed_result(
                exc,
            )

        self._dispatch(
            self._complete_remove,
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
        if not self._store.ready:
            self._finish_operation(
                operation,
                _offline_result(),
            )
            return

        self._track_task(
            self._run_mutation(
                self._signal_async,
                operation,
                entity,
                signal,
                data,
            )
        )

    async def _signal_async(
        self,
        operation: StateOperation,
        entity: EntityState[Any],
        signal: str,
        data: Any,
    ) -> None:
        try:
            address = await self._entity_address(
                entity,
            )

            response = await self._store.signal.emit(
                address,
                signal,
                data,
                id=entity.row_id,
            )

            if response.ok:
                result = StateResult(
                    ok=True,
                    changed=True,
                    data=response.data,
                )
            else:
                result = StateResult(
                    ok=False,
                    changed=False,
                    error=response.error,
                )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            result = self._failed_result(
                exc,
            )

        self._dispatch(
            self._finish_operation,
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
        if not self._store.ready:
            self._complete_state_transaction(
                transaction,
                _offline_result(),
            )
            return

        self._track_task(
            self._run_mutation(
                self._transaction_async,
                transaction,
            )
        )

    async def _transaction_async(
        self,
        state_transaction: StateTransaction,
    ) -> None:
        try:
            transaction = Transaction()
            signals: list[dict[str, Any]] = []

            for action in state_transaction.actions:
                entity = action.entity
                address = await self._entity_address(
                    entity,
                )

                if action.kind is StateOperationKind.SIGNAL:
                    if action.signal is None:
                        continue

                    signals.append(
                        {
                            "address": address.to_list(),
                            "signal": action.signal,
                            "id": entity.row_id,
                            "data": action.data,
                        }
                    )
                    continue

                if action.kind is StateOperationKind.UPDATE:
                    for field_name, value in (
                        action.values or {}
                    ).items():
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
                        else:
                            transaction.set(
                                address,
                                field_name,
                                value,
                            )

                    continue

                if action.kind is StateOperationKind.INSERT:
                    transaction.insert(
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

                transaction.remove(
                    address,
                    row_id,
                )

            result = await self._commit(
                transaction,
                signals=signals,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            result = self._failed_result(
                exc,
            )

        self._dispatch(
            self._complete_state_transaction,
            state_transaction,
            result,
        )

    async def _commit(
        self,
        transaction: Transaction,
        *,
        signals: list[dict[str, Any]] | None = None,
    ) -> StateResult:
        response = await self._store.commit(
            transaction,
            signals=signals,
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
            changed=(
                _transaction_changed(data)
                or bool(signals)
            ),
            data=data,
        )

    # -------------------------------------------------
    # Address helpers
    # -------------------------------------------------

    async def _entity_address(
        self,
        entity: EntityState[Any],
    ) -> Address[Any]:
        address = entity.address

        if address is not None:
            return address

        return await self._resolve_address(
            entity.schema.name,
        )

    # -------------------------------------------------
    # Disposal
    # -------------------------------------------------

    def _dispose_view(
        self,
        view: View,
    ) -> None:
        if isinstance(view, SingleStateView):
            self._single_buffers.pop(
                view,
                None,
            )
            self._single_revisions.pop(
                view,
                None,
            )

        super()._dispose_view(
            view,
        )

    def _dispose_subscription(
        self,
        subscription: Any,
    ) -> None:
        if subscription is not None:
            self._track_task(
                subscription.dispose(),
            )

    def _dispose_signal_binding(
        self,
        binding: Any,
    ) -> None:
        if binding is not None:
            self._track_task(
                binding.dispose(),
            )

    # -------------------------------------------------
    # Properties
    # -------------------------------------------------

    @property
    def store(self) -> RemoteStore:
        return self._store


def _transaction_changed(
    result: TransactionResult,
) -> bool:
    return bool(
        result.inserted
        or result.removed
        or result.updated
    )


def _offline_result() -> StateResult:
    return StateResult(
        ok=False,
        changed=False,
        error=StoreStateError(
            "Remote state is not synchronized"
        ),
    )
