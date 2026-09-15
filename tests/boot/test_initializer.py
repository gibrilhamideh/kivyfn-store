from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypeAlias

import pytest

from kivyfn.store.builder import Branch, Builder, HistoryConfig, Initializer
from kivyfn.store.builder.migration import Migration
from kivyfn.store.core.boot import StoreBoot
from kivyfn.store.error import (
    FieldValidationError,
    ForeignKeyNotFoundError,
    StoreStateError,
)
from kivyfn.store.operations import OpCode
from kivyfn.store.protocol.message.requests import RemoveRequestAction
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.fields import DataField, ForeignKeyField, NumericField
from kivyfn.store.schema.policy import ForeignKeyPolicy
from kivyfn.store.schema.trigger import trigger
from kivyfn.store.storage import FileStorage
from kivyfn.store.store import Store
from kivyfn.store.transaction import Transaction
from tests.core.helpers import insert, setrow

Policy: TypeAlias = Literal["raise", "recover", "erase"]


class Settings(SingleSchema):
    persistent = True
    track_history = True
    value = NumericField(default=0)
    payload = DataField[Any](default={"items": []})
    selected = ForeignKeyField("Runtime")


TRIGGER_CALLS: list[str] = []


class Parents(ManySchema):
    persistent = True
    track_history = True
    value = NumericField(default=1)

    @trigger(OpCode.INSERT)
    def create_child(self, tx: Transaction, ctx: TriggerContext) -> None:
        assert ctx.id is not None
        TRIGGER_CALLS.append(ctx.id)
        runtime = ctx.engine.get_node_by_schema("Runtime")
        tx.insert(runtime.address, {"parent": ctx.id}, ctx.id)


class Runtime(ManySchema):
    parent = ForeignKeyField("Parents", on_delete=ForeignKeyPolicy.CASCADE)
    reading = NumericField(default=0)


class Status(SingleSchema):
    value = NumericField(default=7)


class Seed(Initializer):
    def run(self, data: dict[str, Any]) -> dict[str, Any]:
        data["Runtime"] = {
            row_id: {"parent": row_id} for row_id in data.get("Parents", {})
        }
        data["Status"] = {"value": 12}
        return data


def make(
    path: Path,
    initializer: Initializer | None = None,
    *,
    policy: Policy = "raise",
    migrations: list[Migration] | None = None,
    version: int = 1,
) -> Store:
    tree = (
        Branch(Settings)
        .child("parents", Branch(Parents))
        .child("runtime", Branch(Runtime))
        .child("status", Branch(Status))
    )
    config = (
        Builder()
        .tree(tree)
        .storage_path(path)
        .boot_policy(policy)
        .store_version(version)
        .migrations(migrations or [])
        .initializer(initializer)
        .history(HistoryConfig())
        .build()
    )
    return Store(config)


def save(path: Path, values: dict[str, Any], version: int = 1) -> None:
    FileStorage(path).write_store(values, store_version=version)


def test_initialization_follows_migration_before_defaults_and_relations(
    tmp_path: Path,
) -> None:
    save(tmp_path, {"Settings": {"value": "old"}})
    seen = []

    class ToTwo(Migration):
        version = 2

        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            seen.append("migration")
            data["Settings"]["value"] = "migrated"
            data["Parents"] = {"p": {}}
            return data

    class Prepare(Seed):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            seen.append("initializer")
            assert data["Settings"] == {"value": "migrated"}
            assert data["Parents"] == {"p": {}}
            data["Settings"] = {"value": 9, "selected": "p"}
            return super().run(data)

    instance = make(tmp_path, Prepare(), migrations=[ToTwo()], version=2)
    TRIGGER_CALLS.clear()
    instance.boot()
    assert seen == ["migration", "initializer"]
    assert instance.snapshot_by_schema("Parents") == {"p": {"value": 1}}
    assert instance.snapshot_by_schema("Runtime") == {
        "p": {"parent": "p", "reading": 0}
    }
    assert instance.snapshot_by_schema("Status") == {"value": 12}
    assert instance.snapshot_by_schema("Settings") == {
        "value": 9,
        "selected": "p",
        "payload": {"items": []},
    }
    runtime = instance._engine.get_node_by_schema("Runtime")
    assert runtime.lookup_all("parent", "p") == {"p"}
    assert not TRIGGER_CALLS
    assert set(instance.schema_revisions.values()) == {0}
    assert instance.history().status.total_entries == 0
    saved = FileStorage(tmp_path).load_store([])
    assert set(saved) == {"Settings", "Parents"}
    assert saved["Settings"]["metadata"]["store_version"] == 2


def test_initializer_input_is_detached_and_changes_are_saved(
    tmp_path: Path,
) -> None:
    save(
        tmp_path,
        {
            "Settings": {
                "value": 0,
                "payload": {"items": [1]},
                "selected": None,
            },
            "Parents": {},
        },
    )
    storage = FileStorage(tmp_path)
    generation = storage._manifest_name()
    retained = []

    class Prepare(Initializer):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            data["Settings"]["payload"]["items"].append(2)
            retained.append(data)
            return data

    instance = make(tmp_path, Prepare())
    instance.boot()
    assert storage._manifest_name() != generation
    saved = storage.read("Settings")
    assert saved is not None
    assert saved["data"]["payload"] == {"items": [1, 2]}
    retained[0]["Settings"]["payload"]["items"].append(3)
    assert instance.snapshot_by_schema("Settings", key="payload") == {
        "items": [1, 2]
    }


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
@pytest.mark.parametrize("failure", ["raise", "return"])
def test_initializer_fault_never_erases_or_saves(
    tmp_path: Path, policy: Policy, failure: str
) -> None:
    save(tmp_path, {"Settings": {"value": 6}})
    storage = FileStorage(tmp_path)
    before = storage.load_store([])
    generation = storage._manifest_name()
    calls = []

    class Broken(Initializer):
        def run(self, data: dict[str, Any]) -> Any:
            calls.append(data)
            data["Settings"]["value"] = 99
            if failure == "raise":
                raise RuntimeError("preparation failed")
            return []

    instance = make(tmp_path, Broken(), policy=policy)
    error = RuntimeError if failure == "raise" else TypeError
    with pytest.raises(error):
        instance.boot()
    assert not instance.booted
    assert len(calls) == 1
    assert storage._manifest_name() == generation
    assert storage.load_store([]) == before


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
@pytest.mark.parametrize("failure", ["field", "foreign_key"])
def test_initializer_schema_data_uses_existing_boot_policy(
    tmp_path: Path, policy: Policy, failure: str
) -> None:
    save(tmp_path, {"Settings": {"value": 6}})
    storage = FileStorage(tmp_path)
    generation = storage._manifest_name()

    class Invalid(Initializer):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            if failure == "field":
                data["Settings"]["value"] = "invalid"
            else:
                data["Runtime"] = {"bad": {"parent": "missing"}}
            return data

    instance = make(tmp_path, Invalid(), policy=policy)
    if policy == "raise":
        error = (
            FieldValidationError
            if failure == "field"
            else ForeignKeyNotFoundError
        )
        with pytest.raises(error):
            instance.boot()
        assert not instance.booted
        assert storage._manifest_name() == generation
    else:
        instance.boot()
        expected = 0 if failure == "field" else 6
        assert instance.snapshot_by_schema("Settings", key="value") == (
            expected
        )
        assert instance.snapshot_by_schema("Runtime") == {}


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
def test_only_erase_reinitializes_fresh_candidate_after_unrecoverable_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    save(tmp_path, {"Settings": {"value": 6}})
    seen = []

    class Prepare(Seed):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            seen.append(set(data))
            data.setdefault("Parents", {})["seed"] = {}
            return super().run(data)

    def fail(self: StoreBoot, data: dict[str, Any]) -> None:
        raise RuntimeError("unrecoverable build")

    monkeypatch.setattr(StoreBoot, "_build_nodes", fail)
    instance = make(tmp_path, Prepare(), policy=policy)
    if policy != "erase":
        with pytest.raises(RuntimeError, match="unrecoverable"):
            instance.boot()
        assert seen == [{"Settings"}]
        return

    instance.boot()
    assert seen == [{"Settings"}, set()]
    assert instance.snapshot_by_schema("Settings", key="value") == 0
    assert instance.snapshot_by_schema("Runtime") == {
        "seed": {"parent": "seed", "reading": 0}
    }


@pytest.mark.parametrize("failure", ["raise", "return", "field"])
def test_bad_erase_initializer_fallback_fails_once_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    save(tmp_path, {"Settings": {"value": 6}})
    storage = FileStorage(tmp_path)
    generation = storage._manifest_name()
    calls = []

    class Prepare(Initializer):
        def run(self, data: dict[str, Any]) -> Any:
            calls.append(set(data))
            if data:
                return data
            if failure == "raise":
                raise RuntimeError("fallback failed")
            if failure == "return":
                return []
            return {"Settings": {"value": "invalid"}}

    def fail(self: StoreBoot, data: dict[str, Any]) -> None:
        raise RuntimeError("unrecoverable build")

    monkeypatch.setattr(StoreBoot, "_build_nodes", fail)
    instance = make(tmp_path, Prepare(), policy="erase")
    errors = {
        "raise": RuntimeError,
        "return": TypeError,
        "field": FieldValidationError,
    }
    with pytest.raises(errors[failure]):
        instance.boot()
    assert calls == [{"Settings"}, set()]
    assert not instance.booted
    assert storage._manifest_name() == generation


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
@pytest.mark.parametrize("enabled", [False, True])
def test_migration_preparation_failure_keeps_existing_erase_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: Policy,
    enabled: bool,
) -> None:
    save(tmp_path, {"Settings": {"value": 6}})
    storage = FileStorage(tmp_path)
    generation = storage._manifest_name()
    calls = []

    class Prepare(Seed):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            calls.append(set(data))
            return super().run(data)

    def fail(
        self: StoreBoot, data: dict[str, Any], current_version: int
    ) -> dict[str, Any]:
        raise RecursionError("migration preparation failed")

    monkeypatch.setattr(StoreBoot, "_migrate", fail)
    initializer = Prepare() if enabled else None
    instance = make(tmp_path, initializer, policy=policy)
    if policy != "erase":
        with pytest.raises(RecursionError, match="migration preparation"):
            instance.boot()
        assert not calls
        assert storage._manifest_name() == generation
        return

    instance.boot()
    assert calls == ([set()] if enabled else [])
    assert instance.snapshot_by_schema("Settings", key="value") == 0
    assert instance.snapshot_by_schema("Status", key="value") == (
        12 if enabled else 7
    )


@pytest.mark.parametrize("policy", ["recover", "erase"])
def test_incremental_recovery_checks_seeded_links_without_reinitializing(
    tmp_path: Path, policy: Policy
) -> None:
    save(tmp_path, {"Parents": {"good": {}, "bad": "malformed"}})
    calls = []

    class Prepare(Seed):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            calls.append(set(data["Parents"]))
            return super().run(data)

    instance = make(tmp_path, Prepare(), policy=policy)
    instance.boot()
    assert calls == [{"good", "bad"}]
    assert instance.snapshot_by_schema("Parents") == {"good": {"value": 1}}
    assert instance.snapshot_by_schema("Runtime") == {
        "good": {"parent": "good", "reading": 0}
    }
    assert instance.history().status.total_entries == 0


def test_missing_initializer_keeps_nonpersistent_disk_and_migration_ignored(
    tmp_path: Path,
) -> None:
    save(tmp_path, {"Settings": {"value": 6}, "Status": {"value": 88}})

    class ToTwo(Migration):
        version = 2

        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            data["Status"] = {"value": 99}
            return data

    instance = make(tmp_path, version=2, migrations=[ToTwo()])
    instance.boot()
    assert instance.snapshot_by_schema("Status") == {"value": 7}
    assert set(FileStorage(tmp_path).load_store([])) == {"Settings", "Parents"}


def test_initializer_runs_for_each_new_store_without_noop_disk_rewrite(
    tmp_path: Path,
) -> None:
    calls = []

    class Prepare(Seed):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            calls.append(set(data))
            return super().run(data)

    initializer = Prepare()
    first = make(tmp_path, initializer)
    first.boot()
    storage = FileStorage(tmp_path)
    generation = storage._manifest_name()
    with pytest.raises(StoreStateError, match="already booted"):
        first.boot()
    second = make(tmp_path, initializer)
    second.boot()
    assert calls == [set(), {"Settings", "Parents"}]
    assert storage._manifest_name() == generation
    assert second.snapshot_by_schema("Status") == {"value": 12}


def test_initialized_children_join_later_cascade_and_recorded_history(
    tmp_path: Path,
) -> None:
    save(tmp_path, {"Parents": {"loaded": {}}})
    instance = make(tmp_path, Seed())
    instance.boot()
    TRIGGER_CALLS.clear()
    assert instance.execute(
        (insert(Parents, "new"),), transaction_id="create"
    ).success
    assert TRIGGER_CALLS == ["new"]
    assert instance.execute(
        (RemoveRequestAction("Parents", "loaded"),), transaction_id="remove"
    ).success
    assert instance.snapshot_by_schema("Runtime") == {
        "new": {"parent": "new", "reading": 0}
    }
    assert instance.undo(
        transaction_id="undo-remove",
        expected_position=instance.history().status.undo_position,
    ).success
    assert set(instance.snapshot_by_schema("Runtime")) == {"new", "loaded"}
    assert TRIGGER_CALLS == ["new"]
    assert instance.redo(
        transaction_id="redo-remove",
        expected_position=instance.history().status.redo_position,
    ).success
    assert instance.execute(
        (setrow(Runtime, "new", reading=4),), transaction_id="reading"
    ).success
    assert instance.undo(
        transaction_id="undo-remove-again",
        expected_position=instance.history().status.undo_position,
    ).success
    before = instance.checkpoint()
    assert not instance.undo(
        transaction_id="guard-new-reading",
        expected_position=instance.history().status.undo_position,
    ).success
    assert instance.checkpoint() == before
