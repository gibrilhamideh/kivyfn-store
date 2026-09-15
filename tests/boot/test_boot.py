from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.builder import Branch, Builder
from kivyfn.store.builder.migration import Migration
from kivyfn.store.core.boot import StoreBoot
from kivyfn.store.core.engine import Engine
from kivyfn.store.error import (
    FieldValidationError,
    ForeignKeyNotFoundError,
    StorageCorruptionError,
    UniqueConstraintError,
)
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.fields import (
    ForeignKeyField,
    NumericField,
    StringField,
)
from kivyfn.store.storage import FileStorage


class First(SingleSchema):
    persistent = True
    value = NumericField(default=0)


class Second(SingleSchema):
    persistent = True
    value = NumericField(default=0)


class Ephemeral(SingleSchema):
    value = NumericField(default=7)


class Rows(ManySchema):
    persistent = True
    label = StringField(default="default")
    count = NumericField(default=1)
    uniques = [("label",)]


class Links(ManySchema):
    persistent = True
    row = ForeignKeyField("Rows")


class Selection(SingleSchema):
    persistent = True
    row = ForeignKeyField("Links")


def engine(
    path: Path,
    *,
    policy: Any = "raise",
    version: int = 1,
    migrations: list[Migration] | None = None,
) -> Engine:
    tree = (
        Branch(First)
        .child("second", Branch(Second))
        .child("ephemeral", Branch(Ephemeral))
        .child("rows", Branch(Rows))
        .child("links", Branch(Links))
        .child("selection", Branch(Selection))
    )
    config = (
        Builder()
        .tree(tree)
        .storage_path(path)
        .store_version(version)
        .boot_policy(policy)
        .migrations(migrations or [])
        .build()
    )
    return Engine(config.root._build_node(), config)


def legacy(path: Path, name: str, data: Any, version: Any = 1) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(
        json.dumps({"metadata": {"store_version": version}, "data": data})
    )


def values(instance: Engine, name: str = "First") -> Any:
    return instance.snapshot_by_schema(name)


def test_fresh_boot_defaults_nonpersistent_nodes_and_noop_restart(
    tmp_path: Path,
) -> None:
    first = engine(tmp_path)
    first.boot()
    assert values(first) == {"value": 0}
    assert values(first, "Rows") == {}
    assert values(first, "Ephemeral") == {"value": 7}
    assert first._storage.read("Ephemeral") is None
    name = first._storage._manifest_name()
    second = engine(tmp_path)
    second.boot()
    assert second._storage._manifest_name() == name


def test_import_selects_highest_legacy_version_and_removes_obsolete_files(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "First", {"value": 10}, 1)
    legacy(tmp_path, "Second", {"value": 20}, 2)
    legacy(tmp_path, "Retired", {"old": 99}, 2)
    legacy(tmp_path, "Ephemeral", {"value": 100}, 2)
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text('{"hello":"world"}')
    instance = engine(tmp_path, version=2)
    instance.boot()
    assert values(instance) == {"value": 0}
    assert values(instance, "Second") == {"value": 20}
    assert values(instance, "Ephemeral") == {"value": 7}
    assert unrelated.exists()
    assert not (tmp_path / "Retired.json").exists()
    assert instance._storage.read("Retired") is None


@pytest.mark.parametrize(
    "content",
    [
        "null",
        "{}",
        '{"metadata":{"store_version":1}}',
        '{"metadata":{"store_version":1},"data":null}',
        '{"metadata":{"store_version":true},"data":{}}',
        '{"metadata":{"store_version":1},"data":[]}',
        "{",
    ],
)
def test_strict_boot_rejects_malformed_envelope_without_overwriting(
    tmp_path: Path, content: str
) -> None:
    target = tmp_path / "First.json"
    target.write_text(content)
    instance = engine(tmp_path)
    with pytest.raises(StorageCorruptionError):
        instance.boot()
    assert not instance.booted
    assert target.read_text() == content
    assert instance._storage._manifest_name() is None


@pytest.mark.parametrize("policy", ["recover", "erase"])
def test_recovery_resets_only_corrupt_schema_and_keeps_valid_values(
    tmp_path: Path, policy: str
) -> None:
    (tmp_path / "First.json").write_text("null")
    legacy(tmp_path, "Second", {"value": 20})
    instance = engine(tmp_path, policy=policy)
    instance.boot()
    assert values(instance) == {"value": 0}
    assert values(instance, "Second") == {"value": 20}


def test_missing_and_extra_fields_rebuilt_under_strict_boot(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "First", {"obsolete": 42})
    legacy(tmp_path, "Rows", {"r": {"label": "a", "extra": 3}})
    instance = engine(tmp_path)
    instance.boot()
    assert values(instance) == {"value": 0}
    assert values(instance, "Rows") == {"r": {"label": "a", "count": 1}}


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
def test_field_validation_obeys_boot_policy(
    tmp_path: Path, policy: str
) -> None:
    legacy(tmp_path, "First", {"value": "bad"})
    legacy(tmp_path, "Second", {"value": 20})
    instance = engine(tmp_path, policy=policy)
    if policy == "raise":
        with pytest.raises(FieldValidationError):
            instance.boot()
    else:
        instance.boot()
        assert values(instance) == {"value": 0}
        assert values(instance, "Second") == {"value": 20}


def test_recovery_rebuilds_indexes_and_preserves_first_conflicting_row(
    tmp_path: Path,
) -> None:
    legacy(
        tmp_path,
        "Rows",
        {
            "first": {"label": "same", "count": 2},
            "duplicate": {"label": "same", "count": 3},
            "malformed": 3,
            "recovered": {"label": "other", "count": "invalid"},
        },
    )
    instance = engine(tmp_path, policy="recover")
    instance.boot()
    assert values(instance, "Rows") == {
        "first": {"label": "same", "count": 2},
        "recovered": {"label": "other", "count": 1},
    }
    node = instance.get_node_by_schema("Rows")
    assert node.lookup_one("label", "same") == "first"
    assert node.lookup_one("label", "other") == "recovered"


def test_strict_boot_rejects_duplicate_unique_rows(tmp_path: Path) -> None:
    legacy(tmp_path, "Rows", {"a": {"label": "same"}, "b": {"label": "same"}})
    with pytest.raises(UniqueConstraintError):
        engine(tmp_path).boot()


def test_foreign_key_recovery_reaches_fixed_point_and_resets_singleton(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "Rows", {"valid": {"label": "valid"}})
    legacy(
        tmp_path,
        "Links",
        {"valid-link": {"row": "valid"}, "bad-link": {"row": "missing"}},
    )
    legacy(tmp_path, "Selection", {"row": "bad-link"})
    instance = engine(tmp_path, policy="recover")
    instance.boot()
    assert values(instance, "Links") == {"valid-link": {"row": "valid"}}
    assert values(instance, "Selection") == {"row": None}
    restart = engine(tmp_path)
    restart.boot()
    assert values(restart, "Selection") == {"row": None}


def test_strict_foreign_key_failure_preserves_saved_data(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "Links", {"bad": {"row": "missing"}})
    path = tmp_path / "Links.json"
    old = path.read_bytes()
    with pytest.raises(ForeignKeyNotFoundError):
        engine(tmp_path).boot()
    assert path.read_bytes() == old


def test_complete_migration_chain_runs_in_version_order_on_owned_data(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    legacy(tmp_path, "Second", {"value": 20})
    seen = []

    class ToTwo(Migration):
        version = 2

        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            seen.append(2)
            data["First"]["value"] += 1
            return data

    class ToThree(Migration):
        version = 3

        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            seen.append(3)
            data["Second"]["value"] = data["First"]["value"] * 2
            return data

    instance = engine(tmp_path, version=3, migrations=[ToThree(), ToTwo()])
    instance.boot()
    assert seen == [2, 3]
    assert values(instance) == {"value": 11}
    assert values(instance, "Second") == {"value": 22}
    restart = engine(tmp_path, version=3, migrations=[ToThree(), ToTwo()])
    restart.boot()
    assert seen == [2, 3]
    assert values(restart, "Second") == {"value": 22}


@pytest.mark.parametrize(
    "failure", ["raise", "return", "missing", "duplicate"]
)
def test_failed_or_incomplete_migration_falls_back_to_original_data(
    tmp_path: Path, failure: str
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    seen = []

    class ToTwo(Migration):
        version = 2

        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            seen.append(2)
            data["First"]["value"] = 99
            return data

    class ToThree(Migration):
        version = 3

        def run(self, data: dict[str, Any]) -> Any:
            if failure == "raise":
                raise RuntimeError("migration failed")
            return []

    migrations: list[Migration] = [ToTwo(), ToThree()]
    if failure == "missing":
        migrations = [ToTwo()]
    elif failure == "duplicate":
        migrations.append(ToTwo())
    instance = engine(tmp_path, version=3, migrations=migrations)
    instance.boot()
    assert values(instance) == {"value": 10}
    assert seen == ([] if failure in {"missing", "duplicate"} else [2])


@pytest.mark.parametrize("version", [None, True, 0, 1, "2"])
def test_invalid_migration_declaration_falls_back_to_original(
    tmp_path: Path, version: Any
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    migration = type("Invalid", (Migration,), {"version": version})()
    instance = engine(tmp_path, version=2, migrations=[migration])
    instance.boot()
    assert values(instance) == {"value": 10}


def test_downgrade_preserves_explicit_existing_fallback_contract(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "First", {"value": 10}, 3)
    instance = engine(tmp_path, version=2)
    instance.boot()
    assert values(instance) == {"value": 10}
    persisted = instance._storage.read("First")
    assert persisted is not None
    assert persisted["metadata"]["store_version"] == 2


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
def test_boot_io_failure_never_triggers_recovery_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    legacy(tmp_path, "Second", {"value": 20})
    legacy(tmp_path, "Retired", {"value": 30})

    def failed(path: Path, text: str) -> None:
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_write_json", staticmethod(failed))
        with pytest.raises(OSError, match="disk unavailable"):
            engine(tmp_path, policy=policy, version=2).boot()
    assert (tmp_path / "Retired.json").exists()
    retry = engine(tmp_path, policy=policy, version=2)
    retry.boot()
    assert values(retry) == {"value": 10}
    assert values(retry, "Second") == {"value": 20}


@pytest.mark.parametrize("step", [1, 2])
def test_interrupted_generation_upgrade_retries_from_complete_old_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    legacy(tmp_path, "Second", {"value": 20})
    engine(tmp_path).boot()
    write = FileStorage._write_json
    calls = 0

    def failed(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == step:
            path.write_text(text[:5])
            raise OSError("interrupted upgrade")
        write(path, text)

    class ToTwo(Migration):
        version = 2

        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            data["First"]["value"] += 1
            data["Second"]["value"] += 1
            return data

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_write_json", staticmethod(failed))
        with pytest.raises(OSError, match="interrupted"):
            engine(tmp_path, version=2, migrations=[ToTwo()]).boot()
    retry = engine(tmp_path, version=2, migrations=[ToTwo()])
    retry.boot()
    assert values(retry) == {"value": 11}
    assert values(retry, "Second") == {"value": 21}


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
def test_corrupt_committed_manifest_obeys_store_recovery_policy(
    tmp_path: Path, policy: str
) -> None:
    first = engine(tmp_path)
    first.boot()
    (first._storage._directory / "current.json").write_text("null")
    instance = engine(tmp_path, policy=policy)
    if policy == "raise":
        with pytest.raises(StorageCorruptionError):
            instance.boot()
    else:
        instance.boot()
        assert values(instance) == {"value": 0}
        persisted = FileStorage(tmp_path).read("First")
        assert persisted is not None
        assert persisted["data"] == {"value": 0}


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
def test_erase_is_only_policy_that_resets_after_unrecoverable_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    legacy(tmp_path, "First", {"value": 10})

    def failed(self: StoreBoot, data: dict[str, Any]) -> None:
        raise RuntimeError("unrecoverable build")

    monkeypatch.setattr(StoreBoot, "_build_nodes", failed)
    instance = engine(tmp_path, policy=policy)
    if policy == "erase":
        instance.boot()
        assert values(instance) == {"value": 0}
    else:
        with pytest.raises(RuntimeError, match="unrecoverable"):
            instance.boot()


def test_recovery_removes_corrupt_legacy_sources_after_commit(
    tmp_path: Path,
) -> None:
    (tmp_path / "First.json").write_text("null")
    instance = engine(tmp_path, policy="recover")
    instance.boot()
    assert not (tmp_path / "First.json").exists()
    persisted = instance._storage.read("First")
    assert persisted is not None
    assert persisted["data"] == {"value": 0}


def test_postcommit_import_sync_failure_keeps_sources_and_installs_new_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    legacy(tmp_path, "Second", {"value": 20})
    fsync = FileStorage._fsync_directory
    calls = 0

    def fail(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("postcommit sync")
        fsync(path)

    instance = engine(tmp_path, version=2)
    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_fsync_directory", staticmethod(fail))
        instance.boot()
    assert values(instance) == {"value": 10}
    assert values(instance, "Second") == {"value": 20}
    assert (tmp_path / "First.json").exists()
    retry = engine(tmp_path, version=2)
    retry.boot()
    assert values(retry, "Second") == {"value": 20}
    assert not (tmp_path / "First.json").exists()


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
def test_read_io_failure_propagates_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    read_json = FileStorage._read_json

    def fail(path: Path) -> Any:
        if path.name == "First.json":
            raise PermissionError("not readable")
        return read_json(path)

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_read_json", staticmethod(fail))
        with pytest.raises(PermissionError, match="not readable"):
            engine(tmp_path, policy=policy).boot()
    assert json.loads((tmp_path / "First.json").read_text())["data"] == {
        "value": 10
    }


def test_invalid_defaults_fail_even_erase_policy(tmp_path: Path) -> None:
    class InvalidDefaults(SingleSchema):
        persistent = True
        value = NumericField(default="invalid")

    config = (
        Builder()
        .tree(Branch(InvalidDefaults))
        .storage_path(tmp_path)
        .boot_policy("erase")
        .build()
    )
    with pytest.raises(FieldValidationError):
        Engine(config.root._build_node(), config).boot()
    assert FileStorage(tmp_path)._manifest_name() is None


def test_recovered_boolean_rewrites_value_even_when_equal_to_integer(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "First", {"value": False})
    instance = engine(tmp_path, policy="recover")
    instance.boot()
    assert type(values(instance)["value"]) is int
    persisted = instance._storage.read("First")
    assert persisted is not None
    assert type(persisted["data"]["value"]) is int


def test_migration_validation_failure_never_overwrites_prior_generation(
    tmp_path: Path,
) -> None:
    legacy(tmp_path, "First", {"value": 10})
    original = engine(tmp_path)
    original.boot()
    generation = original._storage._manifest_name()

    class InvalidValue(Migration):
        version = 2

        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            data["First"]["value"] = "invalid"
            return data

    with pytest.raises(FieldValidationError):
        engine(tmp_path, version=2, migrations=[InvalidValue()]).boot()
    assert FileStorage(tmp_path)._manifest_name() == generation
    restart = engine(tmp_path)
    restart.boot()
    assert values(restart) == {"value": 10}


def test_noop_restart_does_not_depend_on_schema_mapping_order(
    tmp_path: Path,
) -> None:
    first = engine(tmp_path)
    first.boot()
    snapshots = first._storage.load_store([])
    reversed_values = {
        name: snapshots[name]["data"] for name in reversed(snapshots)
    }
    first._storage.write_store(reversed_values, store_version=1)
    generation = first._storage._manifest_name()
    restart = engine(tmp_path)
    restart.boot()
    assert restart._storage._manifest_name() == generation
