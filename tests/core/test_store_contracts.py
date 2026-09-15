from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.core.instruction import SetInstruction
from kivyfn.store.error import (
    InvalidOperationError,
    NodeNotFoundError,
    RowNotFoundError,
    StoreStateError,
)
from kivyfn.store.events import StoreCommit
from kivyfn.store.events.event import SetEvent
from kivyfn.store.protocol.message.requests import (
    RemoveRequestAction,
    SetRequestAction,
    SignalRequestAction,
    SignalRowRequestAction,
)
from kivyfn.store.schema import (
    DataField,
    ManySchema,
    NumericField,
    Signal,
    SingleSchema,
)
from tests.core.helpers import insert, make_store, setrow, setvalue


class Root(SingleSchema):
    number = NumericField(default=0)
    payload = DataField[dict[str, Any]](default={"items": []})
    ping = Signal[Any]()


class Rows(ManySchema):
    number = NumericField(default=0)
    payload = DataField[dict[str, Any]](default={"items": []})
    ping = Signal[Any]()


def test_input_snapshot_compiled_values_and_commit_values_have_ownership(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Rows, root_schema=Root)
    original: dict[str, Any] = {"items": [1]}
    result = store.execute(
        (
            setvalue(Root, payload=original),
            insert(Rows, "a", payload=original),
        ),
        transaction_id="set",
    )
    assert result.success
    assert result.commit is not None
    original["items"].append("caller")
    first = result.commit.events[0]
    assert isinstance(first, SetEvent)
    first.new["items"].append("event")
    snapshot = store.snapshot_by_schema("Rows", id="a")
    snapshot["payload"]["items"].append("snapshot")
    operation = result.compiled[0]
    assert isinstance(operation.instruction, SetInstruction)
    operation.instruction.value["items"].append("instruction")
    assert store.snapshot_by_schema("Root", key="payload") == {"items": [1]}
    assert store.snapshot_by_schema("Rows", id="a", key="payload") == {
        "items": [1]
    }
    result = store.execute(
        (setrow(Rows, "a", payload=original),), transaction_id="setrow"
    )
    original["items"].append("later")
    assert store.snapshot_by_schema("Rows", id="a", key="payload") == {
        "items": [1, "caller", "instruction"]
    }


def test_commit_listeners_cannot_mutate_each_others_event_payloads(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, root_schema=Root)
    received = []

    def corrupt(commit: StoreCommit) -> None:
        event = commit.events[0]
        assert isinstance(event, SetEvent)
        event.new["items"].append("callback")
        commit.schema_revisions.clear()

    store.subscribe_commits(corrupt)
    store.subscribe_commits(received.append)
    result = store.execute(
        (setvalue(Root, payload={"items": [1]}),), transaction_id="listeners"
    )
    assert result.commit is not None
    assert received == [result.commit]
    assert received[0].schema_revisions == {"Root": 1}
    assert store.snapshot_by_schema("Root", key="payload") == {"items": [1]}


def test_default_values_are_independent_between_rows(tmp_path: Path) -> None:
    store = make_store(tmp_path, Rows)
    store.execute(
        (insert(Rows, "a"), insert(Rows, "b")), transaction_id="seed"
    )
    node = store.get_node_by_schema("Rows")
    assert node.value["a"]["payload"] is not node.value["b"]["payload"]
    assert node.value["a"]["payload"] is not Rows.payload.default


def test_callback_error_unsubscribe_and_reentrant_write_preserve_view(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, root_schema=Root)
    observed = []
    removed = []

    def first(commit: StoreCommit) -> None:
        with pytest.raises(StoreStateError, match="during commit delivery"):
            store.execute((setvalue(Root, number=2),), transaction_id="nested")
        unsubscribe()
        raise ValueError("application callback failure")

    store.subscribe_commits(first)
    unsubscribe = store.subscribe_commits(removed.append)
    store.subscribe_commits(
        lambda commit: observed.append(
            (
                commit.schema_revisions,
                store.snapshot_by_schema("Root", key="number"),
            )
        )
    )
    result = store.execute((setvalue(Root, number=1),), transaction_id="outer")
    assert result.success
    assert observed == [({"Root": 1}, 1)]
    assert removed == []
    assert not store.is_shutdown


def test_listener_added_during_delivery_starts_at_next_commit(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, root_schema=Root)
    observed = []

    def add_listener(commit: StoreCommit) -> None:
        unsubscribe()
        store.subscribe_commits(observed.append)

    unsubscribe = store.subscribe_commits(add_listener)
    store.execute((setvalue(Root, number=1),), transaction_id="first")
    assert observed == []
    store.execute((setvalue(Root, number=2),), transaction_id="second")
    assert len(observed) == 1
    assert observed[0].transaction_id == "second"


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (SignalRequestAction("Rows", "ping"), InvalidOperationError),
        (SignalRowRequestAction("Root", "a", "ping"), InvalidOperationError),
        (SignalRequestAction("Root", "unknown"), InvalidOperationError),
        (SignalRowRequestAction("Rows", "a", "ping"), RowNotFoundError),
        (SignalRequestAction("Unknown", "ping"), NodeNotFoundError),
    ],
)
def test_invalid_signal_rejects_retained_mutations_too(
    tmp_path: Path, action: Any, error: type[Exception]
) -> None:
    store = make_store(tmp_path, Rows, root_schema=Root)
    received = []
    store.subscribe_commits(received.append)
    result = store.execute(
        (setvalue(Root, number=1), action), transaction_id="invalid"
    )
    assert isinstance(result.error, error)
    assert store.snapshot_by_schema("Root", key="number") == 0
    assert store.schema_revisions["Root"] == 0
    assert received == []
    assert not store.is_shutdown


@pytest.mark.parametrize("signal_first", [True, False])
def test_signal_target_is_final_transaction_row(
    tmp_path: Path, signal_first: bool
) -> None:
    store = make_store(tmp_path, Rows, root_schema=Root)
    observations = []
    store.subscribe_commits(
        lambda commit: observations.append(
            (commit, store.snapshot_by_schema("Rows", id="a"))
        )
    )
    actions = (
        insert(Rows, "a", number=2),
        SignalRowRequestAction("Rows", "a", "ping"),
    )
    if signal_first:
        actions = actions[::-1]
    result = store.execute(actions, transaction_id="insert-signal")
    assert result.success
    assert result.commit is not None
    assert len(result.commit.signals) == 1
    assert observations[0][1]["number"] == 2
    result = store.execute(
        (
            SignalRowRequestAction("Rows", "a", "ping"),
            RemoveRequestAction("Rows", "a"),
        ),
        transaction_id="remove-signal",
    )
    assert isinstance(result.error, RowNotFoundError)
    assert store.snapshot_by_schema("Rows", id="a") is not None
    assert len(observations) == 1


def test_signal_only_duplicates_are_distinct_without_revisions(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, root_schema=Root)
    received = []
    store.subscribe_commits(received.append)
    payload = {"items": [1]}
    signal = SignalRequestAction("Root", "ping", payload)
    result = store.execute((signal, signal), transaction_id="signals")
    assert result.success
    assert result.commit is not None
    assert len(result.commit.signals) == 2
    assert result.commit.events == ()
    assert result.commit.schema_revisions == {}
    payload["items"].append(2)
    result.commit.signals[0].data["items"].append(3)
    assert result.commit.signals[1].data == {"items": [1]}
    assert received[0].signals[0].data == {"items": [1]}
    assert store.schema_revisions["Root"] == 0


def test_snapshot_cache_is_scoped_to_store_instance(tmp_path: Path) -> None:
    store = make_store(tmp_path, root_schema=Root)
    revision, snapshot = store.snapshot_if_changed("Root", revision=0)
    assert revision == 0 and snapshot is not None
    revision, snapshot = store.snapshot_if_changed(
        "Root", revision=0, store_instance_id=store.instance_id
    )
    assert revision == 0 and snapshot is None
    store.execute((setvalue(Root, number=1),), transaction_id="change")
    revision, snapshot = store.snapshot_if_changed(
        "Root", revision=0, store_instance_id=store.instance_id
    )
    assert revision == 1 and snapshot is not None
    assert snapshot["number"] == 1


def test_store_lifecycle_prevents_use_after_shutdown(tmp_path: Path) -> None:
    store = make_store(tmp_path, root_schema=Root)
    with pytest.raises(StoreStateError, match="already booted"):
        store.boot()
    store.shutdown()
    store.shutdown()
    assert not store.booted
    with pytest.raises(StoreStateError):
        store.snapshot_by_schema("Root")
    with pytest.raises(StoreStateError):
        store.execute((SetRequestAction("Root", ()),), transaction_id="closed")
    with pytest.raises(StoreStateError):
        store.subscribe_commits(lambda commit: None)
    with pytest.raises(StoreStateError):
        store.boot()


def test_concurrent_mutations_have_serial_commit_views(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = make_store(tmp_path, root_schema=Root)
    start = Barrier(3)
    observed = []

    def observe(commit: StoreCommit) -> None:
        event = commit.events[0]
        assert isinstance(event, SetEvent)
        observed.append(
            (
                event.new,
                store.snapshot_by_schema("Root", key="number"),
                commit.schema_revisions["Root"],
                store.schema_revisions["Root"],
            )
        )

    store.subscribe_commits(observe)

    def mutate(value: int):
        start.wait(timeout=5)
        return store.execute(
            (setvalue(Root, number=value),), transaction_id=str(value)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(mutate, 1)
        second = executor.submit(mutate, 2)
        start.wait(timeout=5)
        assert first.result(timeout=5).success
        assert second.result(timeout=5).success

    assert len(observed) == 2
    assert all(
        new == visible and revision == current
        for new, visible, revision, current in observed
    )
    assert [item[2] for item in observed] == [1, 2]


def test_shutdown_from_callback_stops_remaining_delivery(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, root_schema=Root)
    received = []
    store.subscribe_commits(lambda commit: store.shutdown())
    store.subscribe_commits(received.append)
    result = store.execute((setvalue(Root, number=1),), transaction_id="close")
    assert result.success
    assert store.is_shutdown
    assert received == []
