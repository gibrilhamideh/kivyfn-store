from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.error import StorageCorruptionError
from kivyfn.store.storage import FileStorage


def saved(path: Path, name: str, data: Any, version: int = 1) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(
        json.dumps({"metadata": {"store_version": version}, "data": data})
    )


def snapshot(storage: FileStorage) -> dict[str, Any]:
    return {
        name: value["data"] for name, value in storage.load_store([]).items()
    }


def record(storage: FileStorage, name: str) -> Any:
    payload = storage.read(name)
    assert payload is not None
    return payload


def test_complete_generation_replaces_all_schemas_and_preserves_order(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    data = {"A": {"z": {"value": 1}, "a": {"value": 2}}, "B": {"v": 3}}
    assert storage.write_store(data, store_version=2)
    data["A"]["z"]["value"] = 100
    reopened = FileStorage(tmp_path)
    assert list(snapshot(reopened)["A"]) == ["z", "a"]
    assert snapshot(reopened)["A"]["z"]["value"] == 1
    assert record(reopened, "A")["metadata"] == {"store_version": 2}
    assert reopened.exists("A")
    assert not reopened.exists("unknown")
    assert reopened.write_store({"A": {}}, store_version=2)
    assert snapshot(reopened) == {"A": {}}


def test_single_schema_write_preserves_others_and_rejects_mixed_versions(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write("A", {"value": 1}, store_version=1)
    storage.write("B", {"value": 2}, store_version=1)
    with pytest.raises(ValueError, match="write_store"):
        storage.write("A", {"value": 3}, store_version=2)
    assert snapshot(storage) == {"A": {"value": 1}, "B": {"value": 2}}


@pytest.mark.parametrize("step", range(1, 6))
def test_precommit_fault_retains_complete_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: int
) -> None:
    storage = FileStorage(tmp_path)
    old = {"A": {"value": 10}, "B": {"value": 20}}
    storage.write_store(old, store_version=1)
    write = FileStorage._write_json
    fsync = FileStorage._fsync_directory
    replace = os.replace
    count = 0

    def fail() -> None:
        nonlocal count
        count += 1
        if count == step:
            raise OSError("injected before commit")

    def write_fault(path: Path, text: str) -> None:
        fail()
        write(path, text)

    def fsync_fault(path: Path) -> None:
        fail()
        fsync(path)

    def replace_fault(source: Any, target: Any) -> None:
        fail()
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_write_json", staticmethod(write_fault))
        patch.setattr(
            FileStorage, "_fsync_directory", staticmethod(fsync_fault)
        )
        patch.setattr(os, "replace", replace_fault)
        if step < 5:
            with pytest.raises(OSError, match="injected"):
                storage.write_store({"A": {}, "B": {}}, store_version=2)
        else:
            assert not storage.write_store({"A": {}, "B": {}}, store_version=2)
    expected = old if step < 5 else {"A": {}, "B": {}}
    assert snapshot(FileStorage(tmp_path)) == expected
    if step < 5:
        assert storage.write_store({"A": {}, "B": {}}, store_version=2)
        assert snapshot(storage) == {"A": {}, "B": {}}


@pytest.mark.parametrize("file_fsync", [1, 2, 3])
def test_fsync_failure_during_generation_or_manifest_keeps_old_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_fsync: int
) -> None:
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {"value": 10}}, store_version=1)
    fsync = os.fsync
    calls = 0

    def fail(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == file_fsync:
            raise OSError("fsync failed")
        fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(OSError, match="fsync failed"):
            storage.write_store({"A": {"value": 20}}, store_version=1)
    assert snapshot(FileStorage(tmp_path)) == {"A": {"value": 10}}


def test_postcommit_fsync_failure_warns_and_preserves_both_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {"value": 1}}, store_version=1)
    previous = storage._manifest_name()
    fsync = FileStorage._fsync_directory
    calls = 0

    def fail(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        fsync(path)

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_fsync_directory", staticmethod(fail))
        assert not storage.write_store({"A": {"value": 2}}, store_version=1)
    assert snapshot(FileStorage(tmp_path)) == {"A": {"value": 2}}
    assert previous is not None
    assert (storage._directory / previous).exists()
    assert "durability is uncertain" in caplog.text


def test_partial_write_is_never_loaded_or_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {"value": 1}}, store_version=1)

    def partial(path: Path, text: str) -> None:
        path.write_text(text[:10])
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_write_json", staticmethod(partial))
        with pytest.raises(OSError, match="disk full"):
            storage.write_store({"A": {"value": 2}}, store_version=1)
    assert snapshot(FileStorage(tmp_path)) == {"A": {"value": 1}}


def test_uncommitted_generation_and_manifest_temp_are_ignored(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {"value": 1}}, store_version=1)
    name = "generation-uncommitted.json"
    (storage._directory / name).write_text(
        '{"metadata":{"store_version":2},"data":{"A":{"value":2}}}'
    )
    (storage._directory / "current.json.tmp").write_text(
        json.dumps({"generation": name})
    )
    assert snapshot(FileStorage(tmp_path)) == {"A": {"value": 1}}
    storage.write_store({"A": {"value": 3}}, store_version=1)
    assert not (storage._directory / name).exists()


def test_legacy_import_and_cleanup_preserve_unrelated_files(
    tmp_path: Path,
) -> None:
    saved(tmp_path, "A", {"value": 1})
    saved(tmp_path, "Retired", {"old": 2})
    unrelated = tmp_path / "application.json"
    unrelated.write_text('{"logging":true}')
    storage = FileStorage(tmp_path)
    assert set(storage.load_store(["A"])) == {"A", "Retired"}
    storage.write_store({"A": {"value": 1}}, store_version=2)
    storage.cleanup_legacy({"A", "Retired"})
    assert not (tmp_path / "A.json").exists()
    assert not (tmp_path / "Retired.json").exists()
    assert unrelated.read_text() == '{"logging":true}'


@pytest.mark.parametrize("content", ["null", "{}", "[]", '{"data":{}}', "{"])
def test_malformed_legacy_envelopes_differ_from_absent_files(
    tmp_path: Path, content: str
) -> None:
    storage = FileStorage(tmp_path)
    assert storage.read("A") is None
    (tmp_path / "A.json").write_text(content)
    with pytest.raises(StorageCorruptionError):
        storage.read("A")
    assert storage.load_store(["A"], recover=True) == {}


@pytest.mark.parametrize("version", [None, True, False, 0, -1, 1.5, "1"])
def test_invalid_saved_versions_are_rejected(
    tmp_path: Path, version: Any
) -> None:
    saved(tmp_path, "A", {}, version)
    with pytest.raises(StorageCorruptionError):
        FileStorage(tmp_path).read("A")


def test_legacy_valid_live_wins_over_temp_and_valid_temp_recovers(
    tmp_path: Path,
) -> None:
    saved(tmp_path, "A", {"value": 1})
    saved(tmp_path, "B", {"value": 2})
    (tmp_path / "B.json").rename(tmp_path / "A.json.tmp")
    storage = FileStorage(tmp_path)
    assert record(storage, "A")["data"] == {"value": 1}
    (tmp_path / "A.json").write_text("broken")
    assert record(storage, "A")["data"] == {"value": 2}
    assert (tmp_path / "A.json").read_text() == "broken"


@pytest.mark.parametrize(
    "damage", ["manifest", "missing", "generation", "null"]
)
def test_corrupt_active_generation_never_silently_reads_legacy(
    tmp_path: Path, damage: str
) -> None:
    saved(tmp_path, "A", {"value": 99})
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {"value": 1}}, store_version=1)
    name = storage._manifest_name()
    assert name is not None
    if damage == "manifest":
        (storage._directory / "current.json").write_text("{}")
    elif damage == "missing":
        (storage._directory / name).unlink()
    elif damage == "generation":
        (storage._directory / name).write_text("{")
    else:
        (storage._directory / name).write_text("null")
    with pytest.raises(StorageCorruptionError):
        storage.load_store(["A"])


@pytest.mark.parametrize(
    "name", ["../escape", "generation-../escape.json", ""]
)
def test_manifest_cannot_escape_storage_directory(
    tmp_path: Path, name: str
) -> None:
    storage = FileStorage(tmp_path)
    storage._directory.mkdir()
    (storage._directory / "current.json").write_text(
        json.dumps({"generation": name})
    )
    with pytest.raises(StorageCorruptionError):
        storage.load_store([])


def test_read_returns_owned_values_and_retains_only_two_generations(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    for value in range(5):
        storage.write_store({"A": {"values": [value]}}, store_version=1)
    result = record(storage, "A")
    result["data"]["values"].append(99)
    assert record(storage, "A")["data"]["values"] == [4]
    assert len(list(storage._directory.glob("generation-*.json"))) == 2


def test_nonserializable_generation_never_changes_active_manifest(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {}}, store_version=1)
    name = storage._manifest_name()
    with pytest.raises(TypeError):
        storage.write_store({"A": {"value": object()}}, store_version=1)
    assert storage._manifest_name() == name


@pytest.mark.parametrize("point", ["base-sync", "generation", "manifest"])
def test_initial_import_failure_never_consumes_legacy_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    saved(tmp_path, "A", {"value": 10})
    saved(tmp_path, "B", {"value": 20})
    storage = FileStorage(tmp_path)
    write = FileStorage._write_json
    sync = FileStorage._fsync_directory

    def write_fault(path: Path, text: str) -> None:
        is_manifest = path.name == "current.json.tmp"
        if (point == "manifest" and is_manifest) or (
            point == "generation" and not is_manifest
        ):
            raise OSError("interrupted import")
        write(path, text)

    def sync_fault(path: Path) -> None:
        if point == "base-sync" and path == tmp_path:
            raise OSError("interrupted import")
        sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_write_json", staticmethod(write_fault))
        patch.setattr(
            FileStorage, "_fsync_directory", staticmethod(sync_fault)
        )
        with pytest.raises(OSError, match="interrupted import"):
            storage.write_store({"A": {}, "B": {}}, store_version=2)
    assert record(FileStorage(tmp_path), "A")["data"] == {"value": 10}
    assert record(FileStorage(tmp_path), "B")["data"] == {"value": 20}
    assert storage.write_store({"A": {}, "B": {}}, store_version=2)


def test_generation_cleanup_failure_keeps_successful_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {"value": 1}}, store_version=1)
    obsolete = storage._manifest_name()
    storage.write_store({"A": {"value": 2}}, store_version=1)
    unlink = Path.unlink

    def fail(path: Path, missing_ok: bool = False) -> None:
        if path.name == obsolete:
            raise OSError("cleanup unavailable")
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        assert storage.write_store({"A": {"value": 3}}, store_version=1)
    assert snapshot(FileStorage(tmp_path)) == {"A": {"value": 3}}
    assert "Unable to remove obsolete" in caplog.text


def test_legacy_cleanup_requires_durable_manifest_before_deleting_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved(tmp_path, "A", {"value": 1})
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {"value": 2}}, store_version=1)

    def fail(path: Path) -> None:
        raise OSError("directory unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(FileStorage, "_fsync_directory", staticmethod(fail))
        storage.cleanup_legacy({"A"})
    assert (tmp_path / "A.json").exists()
    assert snapshot(FileStorage(tmp_path)) == {"A": {"value": 2}}


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_numbers_are_storage_corruption(
    tmp_path: Path, constant: str
) -> None:
    (tmp_path / "A.json").write_text(
        '{"metadata":{"store_version":1},"data":{"value":' + constant + "}}"
    )
    with pytest.raises(StorageCorruptionError):
        FileStorage(tmp_path).read("A")


def test_committed_generation_recovers_only_malformed_schema_payload(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    storage.write_store({"A": {}, "B": {}}, store_version=1)
    name = storage._manifest_name()
    assert name is not None
    path = storage._directory / name
    payload = json.loads(path.read_text())
    payload["data"] = {"A": None, "B": {"value": 10}}
    path.write_text(json.dumps(payload))
    with pytest.raises(StorageCorruptionError):
        storage.load_store(["A", "B"])
    recovered = storage.load_store(["A", "B"], recover=True)
    assert recovered == {
        "B": {"metadata": {"store_version": 1}, "data": {"value": 10}}
    }


def test_legacy_orphan_temp_schema_is_discovered_for_migration(
    tmp_path: Path,
) -> None:
    saved(tmp_path, "Retired", {"value": 10})
    (tmp_path / "Retired.json").rename(tmp_path / "Retired.json.tmp")
    result = FileStorage(tmp_path).load_store([])
    assert result["Retired"]["data"] == {"value": 10}


def test_generation_schema_names_cannot_escape_legacy_cleanup(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path / "store")
    external = tmp_path / "external.json"
    external.write_text("preserved")
    storage.write_store({"../external": {}}, store_version=1)
    storage.cleanup_legacy({"../external"})
    assert external.read_text() == "preserved"
    assert snapshot(storage) == {"../external": {}}
