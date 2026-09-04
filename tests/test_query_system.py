from __future__ import annotations

import asyncio

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kivyfn.store.adapter.query import QueryAdapter
from kivyfn.store.address import Address
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.core.engine import Engine
from kivyfn.store.local.store import LocalStore
from kivyfn.store.query import (
    Query,
    QueryChangeSet,
    QueryInsertEvent,
    QueryRemoveEvent,
    QueryUpdateEvent,
    field,
    query_from_payload,
    query_to_payload,
)
from kivyfn.store.query.event import diff_query_results
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.remote.transport import (
    EventBatchHandler,
    LifecycleEvent,
    LifecycleHandler,
    TransportHandler,
)
from kivyfn.store.schema import (
    BooleanField,
    ForeignKeyField,
    NumericField,
    Schema,
    StringField,
)
from kivyfn.store.server.query import RemoteQuerySubscriptionManager
from kivyfn.store.server.revision import StoreRevisionTracker
from kivyfn.store.transaction import Transaction


TYPES = Address("types")
COMPONENTS = Address("components")
ASSIGNMENTS = Address("assignments")


class RootSchema(Schema):
    pass


class TypeSchema(Schema):
    many = True

    category = StringField(default="unknown")


class ComponentSchema(Schema):
    many = True

    name = StringField(default="")
    enabled = BooleanField(default=True)
    type_id = ForeignKeyField("TypeSchema")


class AssignmentSchema(Schema):
    many = True

    component_id = ForeignKeyField("ComponentSchema")
    channel = NumericField(default=0)


def build_engine(path: Path) -> Engine:
    root = Branch(RootSchema)
    root.child("types", Branch(TypeSchema))
    root.child("components", Branch(ComponentSchema))
    root.child("assignments", Branch(AssignmentSchema))
    node, config = (
        Builder()
        .tree(root)
        .storage_path(path)
        .build()
    )
    engine = Engine(node, config)
    engine.boot()
    seed_engine(engine)
    return engine


def build_store(path: Path) -> LocalStore:
    root = Branch(RootSchema)
    root.child("types", Branch(TypeSchema))
    root.child("components", Branch(ComponentSchema))
    root.child("assignments", Branch(AssignmentSchema))
    node, config = (
        Builder()
        .tree(root)
        .storage_path(path)
        .build()
    )
    store = LocalStore(node, config)
    store.boot()
    seed_store(store)
    return store


def seed_engine(engine: Engine) -> None:
    result = engine.execute(seed_transaction().commands)
    assert result.success


def seed_store(store: LocalStore) -> None:
    result = store.commit(seed_transaction())
    assert result.ok


def seed_transaction() -> Transaction:
    return (
        Transaction()
        .insert(
            TYPES,
            {"category": "light"},
            id="type-light",
        )
        .insert(
            TYPES,
            {"category": "fan"},
            id="type-fan",
        )
        .insert(
            COMPONENTS,
            {
                "name": "Front light",
                "enabled": True,
                "type_id": "type-light",
            },
            id="component-light",
        )
        .insert(
            COMPONENTS,
            {
                "name": "Tunnel fan",
                "enabled": True,
                "type_id": "type-fan",
            },
            id="component-fan",
        )
        .insert(
            ASSIGNMENTS,
            {
                "component_id": "component-light",
                "channel": 7,
            },
            id="assignment-light",
        )
        .insert(
            ASSIGNMENTS,
            {
                "component_id": "component-fan",
                "channel": 2,
            },
            id="assignment-fan",
        )
        .insert(
            ASSIGNMENTS,
            {
                "component_id": None,
                "channel": 9,
            },
            id="assignment-empty",
        )
    )


def lighting_query() -> Query:
    return (
        Query(ASSIGNMENTS)
        .join("component_id", as_="component")
        .join("component.type_id", as_="type")
        .where(
            field("type.category") == "light",
            field("channel") >= 4,
        )
    )


def test_query_codec_round_trip() -> None:
    query = (
        Query(ASSIGNMENTS)
        .outerjoin("component_id", as_="component")
        .where(
            (field("channel") >= 4)
            & field("component.name").not_in(("Unused",)),
            ~(field("channel") == 8),
        )
    )

    assert query_from_payload(query_to_payload(query)) == query


def test_fetch_join_nested_join_and_where(tmp_path: Path) -> None:
    store = build_store(tmp_path)

    try:
        response = store.fetch(lighting_query())
        assert response.ok is True
        assert set(response.data) == {"assignment-light"}
        row = response.data["assignment-light"]
        assert row["component"]["name"] == "Front light"
        assert row["type"]["category"] == "light"
    finally:
        store.shutdown()


def test_where_supports_logical_and_membership(
    tmp_path: Path,
) -> None:
    store = build_store(tmp_path)

    try:
        query = Query(ASSIGNMENTS).where(
            (
                field("channel").in_((2, 7))
                | (field("channel") > 8)
            )
            & ~(field("channel") == 7)
        )
        response = store.fetch(query)
        assert response.ok is True
        assert set(response.data) == {
            "assignment-empty",
            "assignment-fan",
        }
    finally:
        store.shutdown()


def test_inner_and_outer_join_missing_relation(
    tmp_path: Path,
) -> None:
    store = build_store(tmp_path)

    try:
        inner = store.fetch(
            Query(ASSIGNMENTS).join(
                "component_id",
                as_="component",
            )
        )
        outer = store.fetch(
            Query(ASSIGNMENTS).outerjoin(
                "component_id",
                as_="component",
            )
        )

        assert inner.ok is True
        assert outer.ok is True

        assert "assignment-empty" not in inner.data
        assert outer.data["assignment-empty"]["component"] is None
    finally:
        store.shutdown()


def test_invalid_join_and_alias_are_rejected(
    tmp_path: Path,
) -> None:
    engine = build_engine(tmp_path)

    with pytest.raises(ValueError, match="not a foreign key"):
        engine.compile_query(
            Query(ASSIGNMENTS).join(
                "channel",
                as_="component",
            )
        )

    with pytest.raises(ValueError, match="already used"):
        (
            Query(ASSIGNMENTS)
            .join(
                "component_id",
                as_="component",
            )
            .join(
                "component_id",
                as_="component",
            )
        )


def test_local_subscription_reports_view_transitions(
    tmp_path: Path,
) -> None:
    store = build_store(tmp_path)
    query = (
        Query(ASSIGNMENTS)
        .join(
            "component_id",
            as_="component",
        )
        .where(
            field("component.name") == "Selected"
        )
    )
    changes: list[QueryChangeSet] = []

    try:
        subscription = store.subscribe(
            query,
            changes.append,
        )
        assert not subscription.data

        result = store.commit(
            Transaction().setrow(
                COMPONENTS,
                "component-light",
                "name",
                "Selected",
            )
        )
        assert result.ok
        assert isinstance(
            changes[-1].events[0],
            QueryInsertEvent,
        )

        result = store.commit(
            Transaction().setrow(
                ASSIGNMENTS,
                "assignment-light",
                "channel",
                8,
            )
        )
        assert result.ok
        assert isinstance(
            changes[-1].events[0],
            QueryUpdateEvent,
        )

        result = store.commit(
            Transaction().setrow(
                COMPONENTS,
                "component-light",
                "name",
                "Other",
            )
        )
        assert result.ok
        assert isinstance(
            changes[-1].events[0],
            QueryRemoveEvent,
        )
        assert not subscription.data
    finally:
        store.shutdown()


def test_server_query_sequence_advances_without_view_change(
    tmp_path: Path,
) -> None:
    engine = build_engine(tmp_path)
    manager = RemoteQuerySubscriptionManager(
        engine,
        StoreRevisionTracker(),
    )
    result = manager.subscribe(
        "client",
        Query(ASSIGNMENTS).where(
            field("channel") > 100
        ),
    )
    execution = engine.execute(
        Transaction().setrow(
            ASSIGNMENTS,
            "assignment-fan",
            "channel",
            3,
        ).commands
    )
    messages = manager.dispatch(
        execution.events,
    )

    assert len(messages) == 1

    change = QueryChangeSet.from_payload(
        messages[0].payload["query_change"]
    )

    assert change.sequence == 1
    assert not change.events

    snapshot = manager.sync_snapshot(
        "client",
        result.subscription_token,
    )

    assert snapshot.sequence == 1


class QueryFakeTransport:
    def __init__(self) -> None:
        self._connected = False
        self._event_handler: EventBatchHandler | None = None
        self._signal_handler: TransportHandler | None = None
        self._lifecycle_handler: LifecycleHandler | None = None
        self.instance_id = uuid4().hex
        self.data: dict[str, dict[str, Any]] = {}
        self.registrations: dict[str, dict[str, Any]] = {}
        self.publish_during_sync: (
            dict[str, dict[str, Any]] | None
        ) = None
        self._counter = 0

    def set_event_handler(
        self,
        handler: EventBatchHandler,
    ) -> None:
        self._event_handler = handler

    def set_signal_handler(
        self,
        handler: TransportHandler,
    ) -> None:
        self._signal_handler = handler

    def set_lifecycle_handler(
        self,
        handler: LifecycleHandler,
    ) -> None:
        self._lifecycle_handler = handler

    async def connect(self) -> None:
        if self._connected:
            return

        self._connected = True
        self._notify("connected")

    async def disconnect(self) -> None:
        if not self._connected:
            return

        self._connected = False
        self.registrations.clear()
        self._notify("disconnected")

    async def call(
        self,
        event: str,
        message: Any | None = None,
    ) -> Any:
        if not self._connected:
            raise RuntimeError(
                "transport is disconnected"
            )

        payload = (
            message
            if isinstance(message, dict)
            else {}
        )

        if event == "query_fetch":
            return self._success(
                deepcopy(self.data)
            )

        if event == "query_subscribe":
            token = self._token("query")
            event_token = self._token("event")

            self.registrations[token] = {
                "event_token": event_token,
                "sequence": 0,
                "query": deepcopy(
                    payload["query"]
                ),
            }

            return self._success(
                {
                    "subscription_token": token,
                    "event_token": event_token,
                }
            )

        if event == "query_unsubscribe":
            self.registrations.pop(
                payload["subscription_token"],
                None,
            )
            return self._success(True)

        if event == "query_sync":
            registration = self.registrations[
                payload["subscription_token"]
            ]

            snapshot = {
                "server_instance_id": self.instance_id,
                "sequence": registration["sequence"],
                "data": deepcopy(self.data),
            }

            pending = self.publish_during_sync
            self.publish_during_sync = None

            if pending is not None:
                await self.publish(pending)

            return self._success(snapshot)

        if event == "query_audit":
            return self._success(
                {
                    "server_instance_id": self.instance_id,
                    "subscriptions": [
                        {
                            "subscription_token": token,
                            "sequence": self.registrations[
                                token
                            ]["sequence"],
                            "dirty": False,
                        }
                        for token
                        in payload["subscription_tokens"]
                        if token in self.registrations
                    ],
                }
            )

        raise AssertionError(
            f"Unsupported fake call: {event}"
        )

    async def publish(
        self,
        current: dict[str, dict[str, Any]],
        *,
        sequence_step: int = 1,
    ) -> None:
        previous = deepcopy(self.data)
        self.data = deepcopy(current)
        handler = self._event_handler

        if handler is None:
            return

        messages: list[dict[str, Any]] = []

        for registration in self.registrations.values():
            registration["sequence"] += sequence_step

            change = QueryChangeSet(
                server_instance_id=self.instance_id,
                sequence=registration["sequence"],
                events=diff_query_results(
                    previous,
                    current,
                ),
            )

            messages.append(
                {
                    "event_token": (
                        registration["event_token"]
                    ),
                    "query_change": change.payload(),
                }
            )

        if messages:
            await handler(messages)

    async def drop(self) -> None:
        await self.disconnect()

    async def reconnect(self) -> None:
        await self.connect()

    def _notify(
        self,
        event: LifecycleEvent,
    ) -> None:
        callback = self._lifecycle_handler

        if callback is not None:
            callback(event)

    def _token(
        self,
        prefix: str,
    ) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    @staticmethod
    def _success(
        data: Any,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "data": data,
            "error": None,
        }

    @property
    def connected(self) -> bool:
        return self._connected


async def wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(
        poll(),
        timeout=timeout,
    )


@pytest.mark.asyncio
async def test_remote_query_initial_sync_replays_buffered_change(
) -> None:
    transport = QueryFakeTransport()

    transport.data = {
        "a": {
            "value": 1,
        }
    }

    transport.publish_during_sync = {
        "a": {
            "value": 2,
        }
    }

    store = RemoteStore(
        "test",
        transport,
        audit_interval=60.0,
        recovery_delay=0.001,
    )

    await store.connect()

    adapter = QueryAdapter(
        store,
        Query(
            Address("values")
        ),
    )

    await adapter.load()

    assert adapter.data["a"]["value"] == 2
    assert adapter.revision == 1
    assert adapter.synchronized

    await adapter.dispose()
    await store.disconnect()


@pytest.mark.asyncio
async def test_remote_query_adapter_recovers_sequence_gap(
) -> None:
    transport = QueryFakeTransport()

    transport.data = {
        "a": {
            "value": 1,
        }
    }

    store = RemoteStore(
        "test",
        transport,
        audit_interval=60.0,
        recovery_delay=0.001,
    )

    await store.connect()

    adapter = QueryAdapter(
        store,
        Query(
            Address("values")
        ),
    )

    await adapter.load()

    await transport.publish(
        {
            "a": {
                "value": 3,
            }
        },
        sequence_step=2,
    )

    await wait_for(
        lambda: (
            adapter.synchronized
            and adapter.data["a"]["value"] == 3
        )
    )

    assert adapter.revision == 2

    await adapter.dispose()
    await store.disconnect()


@pytest.mark.asyncio
async def test_remote_query_adapter_reconnects_and_refetches(
) -> None:
    transport = QueryFakeTransport()

    transport.data = {
        "a": {
            "value": 1,
        }
    }

    store = RemoteStore(
        "test",
        transport,
        audit_interval=60.0,
        recovery_delay=0.001,
    )

    await store.connect()

    adapter = QueryAdapter(
        store,
        Query(
            Address("values")
        ),
    )

    changes: list[QueryUpdateEvent] = []

    adapter.bind(
        on_update=(
            lambda _adapter, event: changes.append(event)
        ),
    )

    await adapter.load()
    await transport.drop()

    transport.data = {
        "a": {
            "value": 2,
        }
    }

    await transport.reconnect()

    await asyncio.wait_for(
        store.client.wait_until_ready(),
        timeout=1.0,
    )

    assert adapter.data["a"]["value"] == 2
    assert changes[-1].new_value["value"] == 2
    assert adapter.synchronized

    await adapter.dispose()
    await store.disconnect()


@pytest.mark.asyncio
async def test_remote_query_audit_restores_missing_registration(
) -> None:
    transport = QueryFakeTransport()

    transport.data = {
        "a": {
            "value": 1,
        }
    }

    store = RemoteStore(
        "test",
        transport,
        audit_interval=0.01,
        recovery_delay=0.001,
    )

    await store.connect()

    adapter = QueryAdapter(
        store,
        Query(
            Address("values")
        ),
    )

    await adapter.load()

    subscription = adapter._subscription
    assert subscription is not None

    old_token = subscription.token

    transport.registrations.pop(
        old_token
    )

    transport.data = {
        "a": {
            "value": 2,
        }
    }

    def recovered() -> bool:
        current = adapter._subscription

        return (
            adapter.synchronized
            and adapter.data["a"]["value"] == 2
            and current is not None
            and current.token != old_token
        )

    await wait_for(recovered)

    await adapter.dispose()
    await store.disconnect()
