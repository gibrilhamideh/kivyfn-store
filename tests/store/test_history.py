from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kivyfn.store.builder import Branch, Builder, HistoryConfig
from kivyfn.store.core.operations import DataRecord
from kivyfn.store.error import StoreErrorCode, StoreStateError
from kivyfn.store.events.event import StoreCommit
from kivyfn.store.operations import OpCode
from kivyfn.store.protocol.message.requests import (
    RemoveRequestAction,
    SignalRequestAction,
    SignalRowRequestAction,
)
from kivyfn.store.schema import ManySchema, Schema, SingleSchema
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.fields import DataField, ForeignKeyField, NumericField
from kivyfn.store.schema.policy import ForeignKeyPolicy
from kivyfn.store.schema.signal import Signal
from kivyfn.store.schema.trigger import trigger
from kivyfn.store.store import Store
from kivyfn.store.store.history import HistoryPosition
from kivyfn.store.transaction import Transaction
from tests.core.helpers import insert, setrow, setvalue


class Root(SingleSchema):
    track_history = True
    value = NumericField(default=0)
    other = NumericField(default=0)
    payload = DataField[Any](default={})
    opened = Signal[dict[str, Any]](undo="closed")
    closed = Signal[dict[str, Any]](undo="opened")
    ping = Signal[None](undo="ping")
    unsupported = Signal()


class Rows(ManySchema):
    track_history = True
    value = NumericField(default=0)
    other = NumericField(default=0)
    opened = Signal[None](undo="closed")
    closed = Signal[None](undo="opened")


class Telemetry(SingleSchema):
    value = NumericField(default=0)
    other = NumericField(default=0)


class UntrackedRows(ManySchema):
    value = NumericField(default=0)
    other = NumericField(default=0)
    uniques = [("value",)]


def make(
    path: Path,
    *schemas: type[Schema],
    root: type[Schema] = Root,
    history: HistoryConfig | None = HistoryConfig(),
) -> Store:
    tree = Branch(root)
    for schema in schemas:
        tree.child(schema.__name__, Branch(schema))
    config = Builder().tree(tree).storage_path(path).build()
    store = Store(replace(config, history=history))
    store.boot()
    return store


def execute(store: Store, *actions: Any, **metadata: Any):
    return store.execute(actions, transaction_id=uuid4().hex, **metadata)


def undo(store: Store):
    return store.undo(
        transaction_id=uuid4().hex,
        expected_position=store.history().status.undo_position,
    )


def redo(store: Store):
    return store.redo(
        transaction_id=uuid4().hex,
        expected_position=store.history().status.redo_position,
    )


def test_default_off_and_configuration_ownership(tmp_path: Path) -> None:
    store = make(tmp_path, history=None)
    assert execute(store, setvalue(Root, value=1)).success
    assert not store.history().status.enabled
    disabled = undo(store)
    assert disabled.error is not None
    assert disabled.error.code == StoreErrorCode.HISTORY_DISABLED
    invalid: tuple[Any, ...] = (0, -1, True, "10")
    for value in invalid:
        with pytest.raises(ValueError):
            HistoryConfig(max_entries=value)
    with pytest.raises(ValueError):
        type(
            "Reserved",
            (SingleSchema,),
            {"track_history": NumericField(default=0)},
        )()

    class Inherited(Root):
        pass

    assert Inherited().track_history
    with pytest.raises(TypeError):
        type("Invalid", (SingleSchema,), {"track_history": "yes"})()


def test_normalized_effects_guard_only_written_fields(tmp_path: Path) -> None:
    store = make(tmp_path, Telemetry)
    assert execute(
        store,
        setvalue(Root, value=1),
        setvalue(Root, value=2),
        setvalue(Telemetry, value=1),
        setvalue(Telemetry, value=0),
    ).success
    assert len(store.history().entries[0].records) == 1
    assert execute(store, setvalue(Telemetry, value=50)).success
    assert undo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 0
    assert store.snapshot_by_schema("Telemetry", key="value") == 50
    assert redo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 2
    assert store.history().status.total_entries == 1


def test_failed_noop_and_net_zero_do_not_discard_redo(tmp_path: Path) -> None:
    store = make(tmp_path, Telemetry)
    assert execute(store, setvalue(Root, value=1)).success
    assert undo(store).success
    revision = store.history().status.revision
    assert execute(store, setvalue(Root, value=0)).success
    assert execute(
        store, setvalue(Root, value=2), setvalue(Root, value=0)
    ).success
    assert not execute(store, setvalue(Root, value="invalid")).success
    assert execute(store, setvalue(Telemetry, value=9)).success
    assert store.history().status.revision == revision
    assert redo(store).success


def test_detached_payloads_records_and_observers(tmp_path: Path) -> None:
    store = make(tmp_path)
    payload = {"nested": [1, {"integer": 2**63 + 13}]}
    assert execute(store, setvalue(Root, payload=payload)).success
    payload["nested"].append(7)
    page = store.history()
    page.entries[0].records[0].after["nested"].append(9)
    assert undo(store).success
    assert redo(store).success
    assert store.snapshot_by_schema("Root", key="payload") == {
        "nested": [1, {"integer": 2**63 + 13}]
    }
    assert isinstance(store.history().entries[0].records[0], DataRecord)


def test_same_values_remove_reinsert_has_no_history_effect(
    tmp_path: Path,
) -> None:
    store = make(tmp_path, Rows)
    assert execute(store, *(insert(Rows, key) for key in "abc")).success
    before = store.snapshot_by_schema("Rows")
    history = store.history()
    assert execute(
        store, RemoveRequestAction("Rows", "b"), insert(Rows, "b")
    ).success
    assert store.snapshot_by_schema("Rows") == before
    assert store.history() == history
    result = undo(store)
    assert result.success
    assert result.commit is not None
    assert len(result.commit.events) == 3
    assert store.snapshot_by_schema("Rows") == {}
    assert result.commit.schema_revisions == {"Rows": 3}
    assert redo(store).success
    assert store.snapshot_by_schema("Rows") == before


def test_structural_guard_preserves_unrelated_row_fields(
    tmp_path: Path,
) -> None:
    store = make(tmp_path, UntrackedRows)
    assert execute(store, insert(UntrackedRows, "a", value=1)).success
    assert execute(
        store, setvalue(Root, value=1), insert(UntrackedRows, "b", value=2)
    ).success
    assert execute(store, setrow(UntrackedRows, "a", other=42)).success
    assert undo(store).success
    assert store.snapshot_by_schema("UntrackedRows") == {
        "a": {"value": 1, "other": 42}
    }
    assert redo(store).success
    assert execute(store, insert(UntrackedRows, "c", value=3)).success
    assert undo(store).success
    assert store.snapshot_by_schema("UntrackedRows") == {
        "a": {"value": 1, "other": 42},
        "c": {"value": 3, "other": 0},
    }
    assert redo(store).success
    assert execute(store, setrow(UntrackedRows, "b", other=43)).success
    snapshot = store.checkpoint()
    assert not undo(store).success
    assert store.checkpoint() == snapshot


def test_removed_row_field_restore_never_resurrects(tmp_path: Path) -> None:
    store = make(tmp_path, UntrackedRows)
    assert execute(store, insert(UntrackedRows, "a")).success
    assert execute(
        store, setvalue(Root, value=1), setrow(UntrackedRows, "a", other=1)
    ).success
    assert execute(store, RemoveRequestAction("UntrackedRows", "a")).success
    assert not undo(store).success
    assert store.snapshot_by_schema("UntrackedRows") == {}


def test_stale_value_and_away_back_policy(tmp_path: Path) -> None:
    store = make(tmp_path, Telemetry)
    assert execute(
        store, setvalue(Root, value=1), setvalue(Telemetry, value=1)
    ).success
    position = store.history().status.undo_position
    assert execute(store, setvalue(Telemetry, value=2)).success
    assert not undo(store).success
    assert execute(store, setvalue(Telemetry, value=1)).success
    assert undo(store).success
    repeated = store.undo(transaction_id="repeat", expected_position=position)
    assert not repeated.success
    assert repeated.error is not None
    assert repeated.error.code == StoreErrorCode.HISTORY_STALE
    wrong = HistoryPosition(
        "other-instance", position.revision, position.entry_id
    )
    assert not store.redo(
        transaction_id="wrong", expected_position=wrong
    ).success


TRIGGER_CALLS: list[str] = []


class X(SingleSchema):
    track_history = True
    value = NumericField(default=0)

    @trigger(OpCode.SET)
    def derive(self, tx: Transaction, ctx: TriggerContext) -> None:
        TRIGGER_CALLS.append("x")
        z = ctx.engine.get_node_by_schema("Z")
        y = ctx.engine.get_node_by_schema("Y")
        tx.set(y.address, "value", max(ctx.value, ctx.single(z)["value"]))


class Z(SingleSchema):
    value = NumericField(default=0)

    @trigger(OpCode.SET)
    def derive(self, tx: Transaction, ctx: TriggerContext) -> None:
        TRIGGER_CALLS.append("z")
        x = ctx.engine.get_node_by_schema("X")
        y = ctx.engine.get_node_by_schema("Y")
        tx.set(y.address, "value", max(ctx.value, ctx.single(x)["value"]))


class Y(SingleSchema):
    value = NumericField(default=0)


def test_accepted_trigger_results_with_current_untracked_input(tmp_path: Path):
    store = make(tmp_path, Z, Y, root=X)
    TRIGGER_CALLS.clear()
    assert execute(store, setvalue(X, value=1)).success
    assert execute(store, setvalue(Z, value=1)).success
    assert len(store.history().entries) == 1
    assert TRIGGER_CALLS == ["x", "z"]
    assert undo(store).success
    assert tuple(store.snapshot_by_schema(key)["value"] for key in "XZY") == (
        0,
        1,
        0,
    )
    assert store.history().status.cursor == 0
    assert redo(store).success
    assert tuple(store.snapshot_by_schema(key)["value"] for key in "XZY") == (
        1,
        1,
        1,
    )
    assert store.history().status.cursor == 1
    assert TRIGGER_CALLS == ["x", "z"]


def test_signal_payload_order_revisions_and_publication_guard(tmp_path: Path):
    store = make(tmp_path)
    payload = {"window": [1]}
    commits: list[StoreCommit] = []
    observations = []
    store.subscribe_commits(commits.append)

    def history_callback(status):
        with pytest.raises(StoreStateError):
            undo(store)
        observations.append((status.cursor, store.snapshot_by_schema("Root")))

    store.subscribe_history(history_callback)
    assert execute(
        store,
        SignalRequestAction("Root", "opened", payload),
        SignalRequestAction("Root", "opened", {"window": [2]}),
    ).success
    payload["window"].append(3)
    assert store.history().status.total_entries == 1
    assert undo(store).success
    assert [signal.signal for signal in commits[-1].signals] == ["closed"] * 2
    assert [signal.data for signal in commits[-1].signals] == [
        {"window": [2]},
        {"window": [1]},
    ]
    assert commits[-1].schema_revisions == {}
    assert redo(store).success
    assert [signal.data for signal in commits[-1].signals] == [
        {"window": [1]},
        {"window": [2]},
    ]
    assert [cursor for cursor, _ in observations] == [1, 0, 1]


def test_signal_barriers_target_lifetimes_and_count_bounds(tmp_path: Path):
    store = make(tmp_path, Rows, history=HistoryConfig(max_entries=2))
    for value in (1, 2, 3):
        assert execute(store, setvalue(Root, value=value)).success
    assert len(store.history().entries) == 2
    assert undo(store).success
    assert undo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 1
    assert not undo(store).success
    assert execute(store, SignalRequestAction("Root", "unsupported")).success
    assert store.history().status.barrier
    assert execute(
        store, insert(Rows, "a"), SignalRowRequestAction("Rows", "a", "opened")
    ).success
    assert store.history().entries == ()
    barrier = store.history().status.barrier
    assert barrier is not None and "lifetime" in barrier
    assert execute(
        store, SignalRowRequestAction("Rows", "a", "opened")
    ).success
    assert undo(store).success


def test_oversized_entry_commits_and_creates_barrier(tmp_path: Path):
    config = HistoryConfig(max_bytes=3000)
    store = make(tmp_path, history=config)
    assert execute(store, setvalue(Root, value=1)).success
    assert execute(
        store, setvalue(Root, payload={"large": "x" * 10000})
    ).success
    assert (
        store.snapshot_by_schema("Root", key="payload")["large"] == "x" * 10000
    )
    assert store.history().entries == ()
    barrier = store.history().status.barrier
    assert barrier is not None and "byte limit" in barrier
    assert store.history().status.retained_bytes <= config.max_bytes


def test_group_net_zero_signals_clients_and_intervening_writes(tmp_path: Path):
    store = make(tmp_path, Telemetry)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(store, setvalue(Root, value=1), **group).success
    entry_id = store.history().entries[-1].id
    assert execute(store, setvalue(Telemetry, value=50)).success
    assert execute(store, setvalue(Root, value=2), **group).success
    assert len(store.history().entries) == 1
    assert store.history().entries[-1].id == entry_id
    assert undo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 0
    assert store.snapshot_by_schema("Telemetry", key="value") == 50
    assert redo(store).success
    assert execute(store, setvalue(Root, value=3), **group).success
    assert len(store.history().entries) == 2
    assert execute(store, setvalue(Root, value=2), **group).success
    assert len(store.history().entries) == 1
    assert execute(store, setvalue(Root, value=3), **group).success
    assert execute(
        store,
        setvalue(Root, value=4),
        origin_client_id="other",
        history_group="gesture",
    ).success
    assert len(store.history().entries) == 3


def test_group_newly_overlapping_untracked_field_splits(tmp_path: Path):
    store = make(tmp_path, Telemetry)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(store, setvalue(Root, value=1), **group).success
    assert execute(store, setvalue(Telemetry, value=7)).success
    assert execute(
        store, setvalue(Root, value=2), setvalue(Telemetry, value=8), **group
    ).success
    assert len(store.history().entries) == 2
    assert undo(store).success
    assert store.snapshot_by_schema("Telemetry", key="value") == 7
    assert store.snapshot_by_schema("Root", key="value") == 1
    assert undo(store).success
    assert store.snapshot_by_schema("Telemetry", key="value") == 7


def test_group_row_lifetime_split_and_full_final_state(tmp_path: Path):
    store = make(tmp_path, Rows)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(store, insert(Rows, "a"), **group).success
    assert execute(
        store, SignalRowRequestAction("Rows", "a", "opened"), **group
    ).success
    assert len(store.history().entries) == 2
    assert undo(store).success
    assert store.snapshot_by_schema("Rows") == {"a": {"value": 0, "other": 0}}
    assert undo(store).success
    assert store.snapshot_by_schema("Rows") == {}


@pytest.mark.parametrize("policy", list(ForeignKeyPolicy))
def test_restore_rejects_new_incoming_children(tmp_path: Path, policy):
    class Parent(ManySchema):
        track_history = True
        value = NumericField(default=0)

    class Child(ManySchema):
        parent = ForeignKeyField("Parent", on_delete=policy)

    store = make(tmp_path, Parent, Child)
    assert execute(store, insert(Parent, "a")).success
    assert execute(store, insert(Child, "new", parent="a")).success
    snapshot = store.checkpoint()
    assert not undo(store).success
    assert store.checkpoint() == snapshot


def test_restoration_keeps_unique_and_cascade_constraints(tmp_path: Path):
    class Parent(ManySchema):
        track_history = True
        value = NumericField(default=0)

    class Child(ManySchema):
        parent = ForeignKeyField("Parent", on_delete=ForeignKeyPolicy.CASCADE)

    store = make(tmp_path, Parent, Child, UntrackedRows)
    assert execute(
        store, insert(Parent, "a"), insert(Child, "b", parent="a")
    ).success
    assert execute(store, RemoveRequestAction("Parent", "a")).success
    assert store.snapshot_by_schema("Child") == {}
    assert undo(store).success
    assert store.snapshot_by_schema("Child") == {"b": {"parent": "a"}}
    assert redo(store).success


def test_unique_restore_conflict_is_atomic(tmp_path: Path):
    store = make(tmp_path, UntrackedRows)
    assert execute(store, insert(UntrackedRows, "a", value=1)).success
    assert execute(
        store, setvalue(Root, value=1), setrow(UntrackedRows, "a", value=2)
    ).success
    assert execute(store, insert(UntrackedRows, "b", value=1)).success
    snapshot = store.checkpoint()
    assert not undo(store).success
    assert store.checkpoint() == snapshot


def test_schema_declarations_validate_inverse_once():
    class Missing(SingleSchema):
        opened = Signal(undo="missing")

    with pytest.raises(ValueError, match="Invalid inverse"):
        Missing()

    class Wrong(SingleSchema):
        opened = Signal[int](undo="closed")
        closed = Signal[str]()

    with pytest.raises(ValueError, match="payload contracts"):
        Wrong()

    class Valid(SingleSchema):
        opened = Signal[int](undo="closed")
        closed = Signal[int]()

    assert Valid().get_signal("closed").undo is None


def test_persistence_failure_never_installs_history_or_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class Persistent(Root):
        persistent = True

    store = make(tmp_path, root=Persistent)
    assert execute(store, setvalue(Persistent, value=1)).success
    before = store.history()
    commits = []
    store.subscribe_commits(commits.append)

    def fail(*args, **kwargs):
        raise OSError("injected generation write failure")

    monkeypatch.setattr(type(store._engine._storage), "write_store", fail)
    with pytest.raises(OSError):
        undo(store)
    assert store.is_shutdown
    assert store._history.state.cursor == before.status.cursor
    assert store._engine.get_node_by_schema("Persistent").value["value"] == 1
    assert commits == []


@pytest.mark.asyncio
async def test_host_ownership_rejects_worker_and_allows_scheduled_operation(
    tmp_path: Path,
):
    store = make(tmp_path)
    loop = asyncio.get_running_loop()
    owner = object()
    store.claim_host(owner, loop)

    def worker():
        with pytest.raises(StoreStateError, match="Host event loop"):
            execute(store, setvalue(Root, value=1))

        async def scheduled():
            return execute(store, setvalue(Root, value=2))

        return asyncio.run_coroutine_threadsafe(scheduled(), loop).result()

    assert (await asyncio.to_thread(worker)).success
    assert store.snapshot_by_schema("Root", key="value") == 2
    store.release_host(owner)
    assert (
        await asyncio.to_thread(execute, store, setvalue(Root, value=3))
    ).success


def test_checkpoint_is_detached_and_subscription_has_no_gap(tmp_path: Path):
    store = make(tmp_path)
    commits = []
    checkpoint, unsubscribe = store.observe_checkpoint(commits.append)
    checkpoint["schemas"]["Root"]["value"] = 10
    assert execute(store, setvalue(Root, value=1)).success
    assert len(commits) == 1
    assert commits[0].schema_revisions == {"Root": 1}
    assert (
        store.schema_catalog()["Root"]["signals"]["opened"]["undo"] == "closed"
    )
    unsubscribe()
    assert execute(store, setvalue(Root, value=2)).success
    assert len(commits) == 1


def test_history_only_publication_guard_and_callback_isolation(tmp_path: Path):
    store = make(tmp_path)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(store, setvalue(Root, value=1), **group).success
    commits = []
    statuses = []
    store.subscribe_commits(commits.append)

    def failing(status):
        with pytest.raises(StoreStateError):
            execute(store, setvalue(Root, value=2))
        with pytest.raises(StoreStateError):
            store.close_history_group("owner")
        raise ValueError("observer failure")

    store.subscribe_history(failing)
    store.subscribe_history(statuses.append)
    before = store.history().status.revision
    store.close_history_group("owner")
    assert statuses[-1].revision == before + 1
    assert commits == []
    assert execute(store, setvalue(Root, value=2), **group).success
    assert store.history().status.total_entries == 2


def test_group_signal_occurrences_keep_net_zero_entry(tmp_path: Path):
    store = make(tmp_path)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(
        store,
        setvalue(Root, value=1),
        SignalRequestAction("Root", "ping"),
        **group,
    ).success
    assert execute(
        store,
        setvalue(Root, value=0),
        SignalRequestAction("Root", "ping"),
        **group,
    ).success
    entry = store.history().entries[0]
    assert entry.records == ()
    assert len(entry.signals) == 2
    result = undo(store)
    assert result.success and result.commit is not None
    assert len(result.commit.signals) == 2
    assert result.commit.schema_revisions == {}


def test_group_late_failure_and_anonymous_owner(tmp_path: Path):
    store = make(tmp_path)
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(store, setvalue(Root, value=1), **group).success
    before = store.history()
    assert not execute(store, setvalue(Root, value="bad"), **group).success
    assert store.history() == before
    assert execute(store, setvalue(Root, value=2), **group).success
    assert len(store.history().entries) == 1
    assert undo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 0
    for value in (1, 2):
        assert execute(
            store, setvalue(Root, value=value), history_group="anonymous"
        ).success
    assert len(store.history().entries) == 2


def test_group_size_splits_complete_signal_transactions(tmp_path: Path):
    store = make(tmp_path, history=HistoryConfig(max_bytes=4200))
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    payload = {"text": "x" * 500}
    for _ in range(3):
        assert execute(
            store, SignalRequestAction("Root", "opened", payload), **group
        ).success
        assert store.history().status.retained_bytes <= 4200
    assert all(len(entry.signals) == 1 for entry in store.history().entries)
    assert store.history().status.barrier is None
    result = undo(store)
    assert result.success and result.commit is not None
    assert len(result.commit.signals) == 1


def test_count_eviction_never_partially_splits_a_group(tmp_path: Path):
    store = make(tmp_path, history=HistoryConfig(max_entries=1))
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    for value in (1, 2, 3):
        assert execute(store, setvalue(Root, value=value), **group).success
    assert len(store.history().entries[0].transaction_ids) == 3
    assert execute(store, setvalue(Root, value=4)).success
    assert len(store.history().entries) == 1
    assert undo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 3
    assert not undo(store).success


def test_no_static_field_validation_or_default_resolution_on_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = make(tmp_path, Rows)
    assert execute(store, insert(Rows, "a", value=10)).success

    def reject(*args, **kwargs):
        raise AssertionError("History must not validate trusted fields again")

    monkeypatch.setattr(Schema, "challenge_field", reject)
    assert undo(store).success
    assert redo(store).success
    assert store.snapshot_by_schema("Rows") == {"a": {"value": 10, "other": 0}}


@pytest.mark.parametrize("direction", ["undo", "redo"])
@pytest.mark.parametrize(
    "boundary", ["write", "replace", "directory", "install"]
)
def test_navigation_persistence_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction, boundary
):
    import os

    from kivyfn.store.node import Node
    from kivyfn.store.storage import FileStorage

    class Persistent(Root):
        persistent = True

    store = make(tmp_path, Telemetry, root=Persistent)
    assert execute(
        store,
        setvalue(Persistent, value=1),
        setvalue(Telemetry, value=1),
        SignalRequestAction("Persistent", "ping"),
    ).success
    if direction == "redo":
        assert undo(store).success
    expected_value = 0 if direction == "undo" else 1
    prior_value = 1 - expected_value
    previous = store.history()
    commits = []
    store.subscribe_commits(commits.append)
    navigate = undo if direction == "undo" else redo

    def fail(*args, **kwargs):
        raise OSError("injected durable boundary")

    with monkeypatch.context() as patch:
        if boundary == "write":
            patch.setattr(FileStorage, "_write_json", staticmethod(fail))
        elif boundary == "replace":
            patch.setattr(os, "replace", fail)
        elif boundary == "install":
            patch.setattr(Node, "install", fail)
        else:
            sync = FileStorage._fsync_directory
            calls = 0

            def fail_after_replace(path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected post-manifest sync")
                sync(path)

            patch.setattr(
                FileStorage,
                "_fsync_directory",
                staticmethod(fail_after_replace),
            )
        if boundary == "directory":
            result = navigate(store)
            assert result.success
            assert len(commits) == 1
            assert (
                store.history().status.revision == previous.status.revision + 1
            )
        else:
            with pytest.raises(OSError):
                navigate(store)
            assert store.is_shutdown
            assert commits == []
            assert store._history.state.cursor == previous.status.cursor
            node = store._engine.get_node_by_schema("Persistent")
            assert node.value["value"] == prior_value
    recovered = make(tmp_path, Telemetry, root=Persistent)
    durable_value = (
        expected_value if boundary in {"directory", "install"} else prior_value
    )
    assert (
        recovered.snapshot_by_schema("Persistent", key="value")
        == durable_value
    )
    assert recovered.snapshot_by_schema("Telemetry", key="value") == 0
    assert recovered.history().entries == ()
    assert recovered.instance_id != store.instance_id


def test_history_preparation_error_occurs_before_durable_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class Persistent(Root):
        persistent = True

    store = make(tmp_path, root=Persistent)
    assert execute(store, setvalue(Persistent, value=1)).success

    def fail(*args, **kwargs):
        raise RuntimeError("injected history preparation failure")

    monkeypatch.setattr(store._history, "prepare", fail)
    with pytest.raises(RuntimeError):
        execute(store, setvalue(Persistent, value=2))
    assert store.is_shutdown
    recovered = make(tmp_path, root=Persistent)
    assert recovered.snapshot_by_schema("Persistent", key="value") == 1


def test_selected_net_zero_root_keeps_untracked_trigger_effect(tmp_path: Path):
    class Derived(SingleSchema):
        count = NumericField(default=0)

    class Selected(SingleSchema):
        track_history = True
        value = NumericField(default=0)

        @trigger(OpCode.SET)
        def count(self, tx: Transaction, ctx: TriggerContext):
            node = ctx.engine.get_node_by_schema("Derived")
            tx.set(node.address, "count", ctx.single(node)["count"] + 1)

    store = make(tmp_path, Derived, root=Selected)
    assert execute(
        store, setvalue(Selected, value=1), setvalue(Selected, value=0)
    ).success
    entry = store.history().entries[0]
    assert [record.schema for record in entry.records] == ["Derived"]
    assert undo(store).success
    assert store.snapshot_by_schema("Derived", key="count") == 0
    assert redo(store).success
    assert store.snapshot_by_schema("Derived", key="count") == 2


def test_untracked_caller_selects_trigger_generated_tracked_change(
    tmp_path: Path,
):
    class Input(SingleSchema):
        value = NumericField(default=0)

        @trigger(OpCode.SET)
        def generate(self, tx: Transaction, ctx: TriggerContext):
            node = ctx.engine.get_node_by_schema("Root")
            tx.set(node.address, "value", ctx.value)

    store = make(tmp_path, Input)
    assert execute(store, setvalue(Input, value=8)).success
    entry = store.history().entries[0]
    assert {record.schema for record in entry.records} == {"Input", "Root"}
    assert undo(store).success
    assert store.snapshot_by_schema("Input", key="value") == 0
    assert store.snapshot_by_schema("Root", key="value") == 0


def test_guarded_browser_style_edits_and_history_status_are_atomic(
    tmp_path: Path,
):
    store = make(tmp_path)
    assert execute(store, setvalue(Root, value=1)).success
    checkpoint = store.checkpoint()
    assert execute(store, setvalue(Root, value=2)).success
    before = store.checkpoint()
    result = execute(
        store,
        setvalue(Root, value=3),
        expected_store_instance_id=checkpoint["store_instance_id"],
        expected_revisions=checkpoint["schema_revisions"],
    )
    assert not result.success
    assert store.checkpoint() == before
    result = execute(
        store, setvalue(Root, value=3), expected_store_instance_id="previous"
    )
    assert not result.success
    assert store.checkpoint() == before


def test_independently_removed_untracked_row_signal_target_conflicts(
    tmp_path: Path,
):
    class Commands(ManySchema):
        value = NumericField(default=0)
        opened = Signal[None](undo="closed")
        closed = Signal[None](undo="opened")

    store = make(tmp_path, Commands)
    assert execute(store, insert(Commands, "a")).success
    assert execute(
        store,
        setvalue(Root, value=1),
        SignalRowRequestAction("Commands", "a", "opened"),
    ).success
    assert execute(store, RemoveRequestAction("Commands", "a")).success
    before = store.checkpoint()
    commits = []
    store.subscribe_commits(commits.append)
    result = undo(store)
    assert not result.success
    assert store.checkpoint() == before
    assert commits == []


def test_all_group_data_and_history_installed_before_compensation(
    tmp_path: Path,
):
    store = make(tmp_path, Rows)
    assert execute(store, insert(Rows, "a", value=10)).success
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(
        store,
        setvalue(Root, value=1),
        SignalRequestAction("Root", "opened", {"step": 1}),
        **group,
    ).success
    assert execute(
        store,
        setrow(Rows, "a", value=20),
        SignalRequestAction("Root", "opened", {"step": 2}),
        **group,
    ).success
    observations = []

    def observe(commit):
        if commit.kind == "undo":
            observations.append(
                (
                    store.snapshot_by_schema("Root", key="value"),
                    store.snapshot_by_schema("Rows", id="a", key="value"),
                    store.history().status.cursor,
                    tuple(signal.data["step"] for signal in commit.signals),
                )
            )

    store.subscribe_commits(observe)
    assert undo(store).success
    assert observations == [(0, 10, 1, (2, 1))]


def test_selected_mixed_signal_without_inverse_is_whole_transaction_barrier(
    tmp_path: Path,
):
    class Commands(SingleSchema):
        unsupported = Signal()

    store = make(tmp_path, Commands)
    assert execute(store, setvalue(Root, value=1)).success
    assert execute(
        store,
        setvalue(Root, value=2),
        SignalRequestAction("Commands", "unsupported"),
    ).success
    assert store.history().entries == ()
    assert store.history().status.barrier is not None
    assert not undo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 2
    assert execute(store, setvalue(Root, value=3)).success
    assert undo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 2


def test_same_id_remove_reinsert_group_restores_precise_fields(tmp_path: Path):
    store = make(tmp_path, Rows)
    assert execute(
        store,
        insert(Rows, "a", value=1),
        insert(Rows, "b", value=2),
        insert(Rows, "c", value=3),
    ).success
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(store, RemoveRequestAction("Rows", "b"), **group).success
    assert execute(store, insert(Rows, "b", value=20), **group).success
    assert execute(store, setrow(Rows, "b", other=21), **group).success
    entry = store.history().entries[-1]
    assert {(record.row_id, record.key) for record in entry.records} == {
        ("b", "value"),
        ("b", "other"),
    }
    assert undo(store).success
    assert store.snapshot_by_schema("Rows") == {
        "a": {"value": 1, "other": 0},
        "b": {"value": 2, "other": 0},
        "c": {"value": 3, "other": 0},
    }
    assert redo(store).success
    assert store.snapshot_by_schema("Rows") == {
        "a": {"value": 1, "other": 0},
        "b": {"value": 20, "other": 21},
        "c": {"value": 3, "other": 0},
    }


def test_group_signal_then_remove_splits_supported_transactions(
    tmp_path: Path,
):
    store = make(tmp_path, Rows)
    assert execute(store, insert(Rows, "a")).success
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(
        store, SignalRowRequestAction("Rows", "a", "opened"), **group
    ).success
    assert execute(store, RemoveRequestAction("Rows", "a"), **group).success
    page = store.history()
    assert page.status.barrier is None
    assert len(page.entries) == 3
    assert len(page.entries[-2].signals) == 1
    assert page.entries[-1].signals == ()
    removed = undo(store)
    assert removed.success and removed.commit is not None
    assert removed.commit.signals == ()
    assert store.snapshot_by_schema("Rows", id="a") is not None
    signalled = undo(store)
    assert signalled.success and signalled.commit is not None
    assert [signal.signal for signal in signalled.commit.signals] == ["closed"]


def test_inverse_inheritance_and_non_signal_target_at_schema_boundary():
    class NotSignal(SingleSchema):
        opened = Signal(undo="value")
        value = NumericField(default=0)

    with pytest.raises(ValueError, match="Invalid inverse"):
        NotSignal()

    class Base(SingleSchema):
        opened = Signal[dict[str, int]](undo="closed")
        closed = Signal[dict[str, int]]()

    class Inherited(Base):
        pass

    assert Inherited().get_signal("opened").undo == "closed"
    assert Inherited().get_signal("closed").undo is None
    # Ordinary Python declaration override removes the inherited signal;
    # inherited inverse names resolve against the final schema declarations.
    hidden = type("HiddenInverse", (Base,), {"closed": None})
    with pytest.raises(ValueError, match="Invalid inverse"):
        hidden()


def test_nested_cascade_restoration_and_new_incoming_reference(tmp_path: Path):
    class Parent(ManySchema):
        track_history = True
        value = NumericField(default=0)

    class Middle(ManySchema):
        parent = ForeignKeyField("Parent", on_delete=ForeignKeyPolicy.CASCADE)

    class Leaf(ManySchema):
        middle = ForeignKeyField("Middle", on_delete=ForeignKeyPolicy.CASCADE)

    store = make(tmp_path, Parent, Middle, Leaf)
    assert execute(
        store,
        insert(Parent, "p"),
        insert(Middle, "m", parent="p"),
        insert(Leaf, "l", middle="m"),
    ).success
    initial = {
        schema: store.snapshot_by_schema(schema)
        for schema in ("Parent", "Middle", "Leaf")
    }
    assert execute(store, RemoveRequestAction("Parent", "p")).success
    deleted = store.history().entries[-1]
    assert {record.schema for record in deleted.records} == set(initial)
    assert all(store.snapshot_by_schema(schema) == {} for schema in initial)
    assert undo(store).success
    assert {
        schema: store.snapshot_by_schema(schema) for schema in initial
    } == initial
    assert redo(store).success
    assert undo(store).success
    assert execute(store, insert(Leaf, "new", middle="m")).success
    before = store.checkpoint()
    # Redo must not expand a new cascade for the independently added leaf.
    assert not redo(store).success
    assert store.checkpoint() == before
    # Undoing the original graph creation is also blocked by that leaf.
    assert not undo(store).success
    assert store.checkpoint() == before


def test_unique_value_swap_restores_complete_final_index_state(tmp_path: Path):
    store = make(tmp_path, UntrackedRows)
    assert execute(
        store,
        insert(UntrackedRows, "a", value=1),
        insert(UntrackedRows, "b", value=2),
    ).success
    assert execute(
        store,
        setvalue(Root, value=1),
        setrow(UntrackedRows, "a", value=2),
        setrow(UntrackedRows, "b", value=1),
    ).success
    assert undo(store).success
    assert store.snapshot_by_schema("UntrackedRows") == {
        "a": {"value": 1, "other": 0},
        "b": {"value": 2, "other": 0},
    }
    assert redo(store).success
    assert store.snapshot_by_schema("UntrackedRows") == {
        "a": {"value": 2, "other": 0},
        "b": {"value": 1, "other": 0},
    }
    assert store.history().status.total_entries == 1


def test_insert_remove_in_one_transaction_is_net_zero_and_keeps_redo(
    tmp_path: Path,
):
    store = make(tmp_path, Rows)
    assert execute(store, setvalue(Root, value=1)).success
    assert undo(store).success
    before = store.history()
    result = execute(
        store,
        insert(Rows, "temporary"),
        RemoveRequestAction("Rows", "temporary"),
    )
    assert result.success and result.commit is not None
    # Original notifications remain ordinary transaction notifications;
    # normalization only removes the ineffective history transformation.
    assert len(result.commit.events) == 2
    assert store.snapshot_by_schema("Rows") == {}
    assert store.history() == before
    assert redo(store).success
    assert store.snapshot_by_schema("Root", key="value") == 1


def test_grouped_insert_remove_does_not_resurrect_discarded_redo(
    tmp_path: Path,
):
    store = make(tmp_path, Rows)
    assert execute(store, setvalue(Root, value=1)).success
    assert undo(store).success
    group = {"origin_client_id": "owner", "history_group": "gesture"}
    assert execute(store, insert(Rows, "temporary"), **group).success
    assert not store.history().status.can_redo
    assert execute(
        store, RemoveRequestAction("Rows", "temporary"), **group
    ).success
    page = store.history()
    assert page.entries == ()
    assert not page.status.can_undo and not page.status.can_redo
    assert store.snapshot_by_schema("Rows") == {}
    assert store.snapshot_by_schema("Root", key="value") == 0


def test_navigation_uses_fresh_commit_and_local_occurrence_identity(
    tmp_path: Path,
):
    from kivyfn.store.host.query import HostQueries
    from kivyfn.store.host.subscription import HostSubscriptions
    from kivyfn.store.protocol.message.event import SignalEventAction
    from kivyfn.store.protocol.message.requests import (
        SingleSubscriptionRequest,
    )

    store = make(tmp_path)
    subscriptions = HostSubscriptions(store, HostQueries(store))
    subscriptions.subscribe_schema("window", SingleSubscriptionRequest("Root"))
    messages = []
    store.subscribe_commits(
        lambda commit: messages.extend(subscriptions.capture(commit))
    )
    original = execute(
        store,
        SignalRequestAction("Root", "opened", {"step": 1}),
        SignalRequestAction("Root", "opened", {"step": 2}),
    )
    inverse = undo(store)
    replayed = redo(store)
    results = (original, inverse, replayed)
    assert all(result.success for result in results)
    assert len(messages) == 3
    transaction_ids = {message.transaction_id for _, message in messages}
    assert len(transaction_ids) == 3 and None not in transaction_ids
    assert transaction_ids == {
        result.commit.transaction_id
        for result in results
        if result.commit is not None
    }
    identities = set()
    payload_orders = []
    for client_id, message in messages:
        assert client_id == "window"
        actions = message.events[0].actions
        signals = [
            item for item in actions if isinstance(item, SignalEventAction)
        ]
        assert [item.occurrence_id for item in signals] == [0, 1]
        payload_orders.append([item.data["step"] for item in signals])
        identities.update(
            (message.transaction_id, item.occurrence_id) for item in signals
        )
    assert len(identities) == 6
    assert payload_orders == [[1, 2], [2, 1], [1, 2]]
