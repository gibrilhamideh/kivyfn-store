from __future__ import annotations

import asyncio
import json

from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.address import Address
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.core.engine import Engine
from kivyfn.store.error import StorageCorruptionError
from kivyfn.store.local.store import LocalStore
from kivyfn.store.node import Node
from kivyfn.store.schema import (
    ForeignKeyField,
    NumericField,
    Schema,
)
from kivyfn.store.server.proxy import StoreServerProxy
from kivyfn.store.server.server import StoreServer
from kivyfn.store.storage import (
    FileStorage,
    PersistenceTracker,
    parse_persist_interval,
)
from kivyfn.store.transaction import Transaction


COUNTER = Address("counter")


class RootSchema(Schema):
    pass


class FastCounterSchema(Schema):
    persistent = True
    persist_interval = "00:00:00.050"

    count = NumericField(default=0)


class LongCounterSchema(Schema):
    persistent = True
    persist_interval = "00:05:00"

    count = NumericField(default=0)


class ImmediateCounterSchema(Schema):
    persistent = True

    count = NumericField(default=0)


def build_root(
    schema: type[Schema],
    path: Path,
) -> tuple[Node, Any]:
    root = Branch(RootSchema).child("counter", Branch(schema))
    return (
        Builder()
        .tree(root)
        .storage_path(path)
        .build()
    )


def read_count(path: Path, schema_name: str) -> int:
    payload = FileStorage(path).read(schema_name)
    assert isinstance(payload, dict)
    return int(payload["data"]["count"])


def test_parse_persist_interval() -> None:
    assert parse_persist_interval(None) is None
    assert parse_persist_interval("00:05:00") == 300.0
    assert parse_persist_interval("01:02:03.500") == 3723.5

    with pytest.raises(ValueError, match="HH:MM:SS"):
        parse_persist_interval("5m")

    with pytest.raises(ValueError, match="greater than zero"):
        parse_persist_interval("00:00:00")

    with pytest.raises(TypeError, match="string or None"):
        parse_persist_interval(300)  # type: ignore[arg-type]


def test_delayed_persistence_requires_persistent_schema(
    tmp_path: Path,
) -> None:
    class InvalidSchema(Schema):
        persist_interval = "00:05:00"

        count = NumericField(default=0)

    node, config = build_root(InvalidSchema, tmp_path)

    with pytest.raises(ValueError, match="persistent is False"):
        Engine(node, config)


def test_delayed_schema_cannot_own_foreign_key(
    tmp_path: Path,
) -> None:
    class TargetSchema(Schema):
        many = True
        persistent = True

        value = NumericField(default=0)

    class DelayedSourceSchema(Schema):
        many = True
        persistent = True
        persist_interval = "00:05:00"

        target_id = ForeignKeyField("TargetSchema")

    root = Branch(RootSchema)
    root.child("target", Branch(TargetSchema))
    root.child("source", Branch(DelayedSourceSchema))
    node, config = (
        Builder()
        .tree(root)
        .storage_path(tmp_path)
        .build()
    )

    with pytest.raises(ValueError, match="cannot participate"):
        Engine(node, config)


def test_delayed_schema_cannot_be_foreign_key_target(
    tmp_path: Path,
) -> None:
    class DelayedTargetSchema(Schema):
        many = True
        persistent = True
        persist_interval = "00:05:00"

        value = NumericField(default=0)

    class ImmediateSourceSchema(Schema):
        many = True
        persistent = True

        target_id = ForeignKeyField("DelayedTargetSchema")

    root = Branch(RootSchema)
    root.child("target", Branch(DelayedTargetSchema))
    root.child("source", Branch(ImmediateSourceSchema))
    node, config = (
        Builder()
        .tree(root)
        .storage_path(tmp_path)
        .build()
    )

    with pytest.raises(ValueError, match="cannot participate"):
        Engine(node, config)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeSchema:
    name = "Counter"


class FakeNode:
    def __init__(self) -> None:
        self.schema = FakeSchema()
        self.value = {
            "count": 0,
        }


def test_first_dirty_deadline_is_not_debounced() -> None:
    clock = FakeClock()
    tracker = PersistenceTracker(clock=clock)
    node = FakeNode()

    tracker.mark_dirty(node, 300.0)  # type: ignore[arg-type]
    assert tracker.next_deadline() == 300.0

    clock.value = 200.0
    node.value["count"] = 1
    tracker.mark_dirty(node, 300.0)  # type: ignore[arg-type]
    assert tracker.next_deadline() == 300.0

    clock.value = 300.0
    snapshots = tracker.capture_due()
    assert len(snapshots) == 1
    assert snapshots[0].generation == 2
    assert snapshots[0].data["count"] == 1


def test_generation_change_during_write_stays_dirty() -> None:
    clock = FakeClock()
    tracker = PersistenceTracker(clock=clock)
    node = FakeNode()

    tracker.mark_dirty(node, 10.0)  # type: ignore[arg-type]
    clock.value = 10.0
    snapshot = tracker.capture_due()[0]

    clock.value = 11.0
    node.value["count"] = 2
    tracker.mark_dirty(node, 10.0)  # type: ignore[arg-type]
    tracker.complete(snapshot, success=True)

    assert tracker.has_pending
    assert tracker.next_deadline() == 21.0
    current = tracker.capture_due(force=True)[0]
    assert current.generation == 2
    assert current.data["count"] == 2


def test_failed_write_remains_dirty_and_retries() -> None:
    clock = FakeClock()
    tracker = PersistenceTracker(
        clock=clock,
        retry_delay=5.0,
    )
    node = FakeNode()

    tracker.mark_dirty(node, 10.0)  # type: ignore[arg-type]
    clock.value = 10.0
    snapshot = tracker.capture_due()[0]
    tracker.complete(snapshot, success=False)

    assert tracker.has_pending
    assert tracker.next_deadline() == 15.0


def test_delayed_local_store_coalesces_latest_value(
    tmp_path: Path,
) -> None:
    node, config = build_root(FastCounterSchema, tmp_path)
    store = LocalStore(node, config)
    store.boot()

    try:
        assert read_count(tmp_path, "FastCounterSchema") == 0
        first = store.commit(Transaction().set(COUNTER, "count", 1))
        second = store.commit(Transaction().set(COUNTER, "count", 2))
        assert first.ok
        assert second.ok
        assert store.engine.pending_persistence_count == 1
        assert read_count(tmp_path, "FastCounterSchema") == 0

        async def wait_saved() -> None:
            while read_count(tmp_path, "FastCounterSchema") != 2:
                await asyncio.sleep(0.01)

        asyncio.run(asyncio.wait_for(wait_saved(), timeout=1.0))
        assert store.engine.pending_persistence_count == 0
    finally:
        store.shutdown()


def test_local_shutdown_flushes_delayed_schema(
    tmp_path: Path,
) -> None:
    node, config = build_root(LongCounterSchema, tmp_path)
    store = LocalStore(node, config)
    store.boot()
    result = store.commit(Transaction().set(COUNTER, "count", 7))
    assert result.ok
    assert read_count(tmp_path, "LongCounterSchema") == 0

    store.shutdown()

    assert read_count(tmp_path, "LongCounterSchema") == 7


def test_immediate_schema_keeps_current_behavior(
    tmp_path: Path,
) -> None:
    node, config = build_root(ImmediateCounterSchema, tmp_path)
    store = LocalStore(node, config)
    store.boot()

    try:
        result = store.commit(Transaction().set(COUNTER, "count", 4))
        assert result.ok
        assert read_count(tmp_path, "ImmediateCounterSchema") == 4
        assert store.engine.pending_persistence_count == 0
    finally:
        store.shutdown()


class FakeServerTransport:
    def __init__(self) -> None:
        self._running = False

    async def start(self, proxy: StoreServerProxy) -> None:
        _ = proxy
        self._running = True

    async def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def endpoint(self) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_server_stop_flushes_delayed_schema(
    tmp_path: Path,
) -> None:
    node, config = build_root(LongCounterSchema, tmp_path)
    server = StoreServer(node, config, FakeServerTransport())
    await server.start()
    execution = server.engine.execute(
        Transaction().set(COUNTER, "count", 9).commands
    )
    assert execution.success
    assert read_count(tmp_path, "LongCounterSchema") == 0

    await server.stop()

    assert read_count(tmp_path, "LongCounterSchema") == 9


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_atomic_storage_write_leaves_no_temp_file(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write("Counter", {"count": 1}, store_version=1)

    assert not (tmp_path / "Counter.json.tmp").exists()
    payload = storage.read("Counter")
    assert isinstance(payload, dict)
    assert payload["data"]["count"] == 1


def test_valid_live_file_wins_and_stale_temp_is_removed(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write("Counter", {"count": 1}, store_version=1)
    temp_path = tmp_path / "Counter.json.tmp"
    temp_path.write_text("{broken", encoding="utf-8")

    payload = storage.read("Counter")
    assert isinstance(payload, dict)
    assert payload["data"]["count"] == 1
    assert not temp_path.exists()


def test_valid_temp_recovers_missing_or_invalid_live_file(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "Counter.json"
    temp_path = tmp_path / "Counter.json.tmp"
    live_path.write_text("{broken", encoding="utf-8")
    write_json(
        temp_path,
        {
            "metadata": {
                "store_version": 1,
            },
            "data": {
                "count": 2,
            },
        },
    )
    storage = FileStorage(tmp_path)

    payload = storage.read("Counter")
    assert isinstance(payload, dict)
    assert payload["data"]["count"] == 2
    assert not temp_path.exists()
    assert json.loads(live_path.read_text())["data"]["count"] == 2


def test_storage_raises_when_live_and_temp_are_corrupt(
    tmp_path: Path,
) -> None:
    (tmp_path / "Counter.json").write_text(
        "{broken",
        encoding="utf-8",
    )
    (tmp_path / "Counter.json.tmp").write_text(
        "also broken",
        encoding="utf-8",
    )
    storage = FileStorage(tmp_path)

    with pytest.raises(StorageCorruptionError, match="Counter"):
        storage.read("Counter")


class FailFirstWriteStorage(FileStorage):
    def __init__(self, base_path: Path) -> None:
        super().__init__(base_path)
        self._failed = False

    def write(
        self,
        schema_name: str,
        data: dict,
        *,
        store_version: int,
    ) -> None:
        if not self._failed:
            self._failed = True
            raise OSError("simulated write failure")

        super().write(
            schema_name,
            data,
            store_version=store_version,
        )


@pytest.mark.asyncio
async def test_server_stop_can_retry_failed_final_flush(
    tmp_path: Path,
) -> None:
    node, config = build_root(LongCounterSchema, tmp_path)
    server = StoreServer(node, config, FakeServerTransport())
    await server.start()
    server.engine._storage = FailFirstWriteStorage(tmp_path)
    execution = server.engine.execute(
        Transaction().set(COUNTER, "count", 11).commands
    )
    assert execution.success

    with pytest.raises(OSError, match="simulated write failure"):
        await server.stop()

    assert server.engine.has_pending_persistence
    await server.stop()

    assert not server.engine.has_pending_persistence
    assert read_count(tmp_path, "LongCounterSchema") == 11


def test_delayed_persistence_keeps_state_and_events_immediate(
    tmp_path: Path,
) -> None:
    node, config = build_root(LongCounterSchema, tmp_path)
    store = LocalStore(node, config)
    store.boot()
    changes: list[Any] = []
    store.subscription.set(
        changes.append,
        COUNTER,
        key="count",
    )

    result = store.commit(Transaction().set(COUNTER, "count", 6))

    assert result.ok
    response = store.snapshot(COUNTER)
    assert response.ok is True
    assert response.data["count"] == 6
    assert changes[-1].new_value == 6
    assert read_count(tmp_path, "LongCounterSchema") == 0
    store.shutdown()
    assert read_count(tmp_path, "LongCounterSchema") == 6
