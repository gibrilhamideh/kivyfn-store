from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from kivyfn.store.error import StorageCorruptionError
from kivyfn.store.storage import FileStorage
from tests.boot.test_boot import engine, legacy, values


@pytest.fixture(params=["integer", "depth"])
def limited_json_value(request: pytest.FixtureRequest) -> Iterator[str]:
    if request.param == "depth":
        # The C JSON decoder's limit can exceed Python's recursion limit.
        depth = 10_000
        yield "[" * depth + "0" + "]" * depth
        return

    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        yield "1" * 4301
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
@pytest.mark.parametrize("source", ["legacy", "generation"])
def test_parser_limits_obey_boot_policy(
    tmp_path: Path, limited_json_value: str, policy: str, source: str
) -> None:
    if source == "legacy":
        legacy(tmp_path, "Second", {"value": 20})
        target = tmp_path / "First.json"
        data = '{"value":' + limited_json_value + "}"
    else:
        storage = FileStorage(tmp_path)
        storage.write_store({"First": {}, "Second": {}}, store_version=1)
        generation = storage._manifest_name()
        assert generation is not None
        target = storage._directory / generation
        data = (
            '{"First":{"value":'
            + limited_json_value
            + '},"Second":{"value":20}}'
        )
    content = '{"metadata":{"store_version":1},"data":' + data + "}"
    target.write_text(content)

    instance = engine(tmp_path, policy=policy)
    if policy == "raise":
        with pytest.raises(StorageCorruptionError):
            instance.boot()
        assert not instance.booted
        assert target.read_text() == content
        return

    instance.boot()
    assert instance.booted
    assert values(instance) == {"value": 0}
    assert values(instance, "Second") == {
        "value": 20 if source == "legacy" else 0
    }
    persisted = FileStorage(tmp_path).read("First")
    assert persisted is not None
    assert persisted["data"] == {"value": 0}
    restart = engine(tmp_path)
    restart.boot()
    assert values(restart) == values(instance)
    assert values(restart, "Second") == values(instance, "Second")


@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
def test_parser_limits_allow_valid_legacy_temporary_fallback(
    tmp_path: Path, limited_json_value: str, policy: str
) -> None:
    (tmp_path / "First.json").write_text(
        '{"metadata":{"store_version":1},"data":{"value":'
        + limited_json_value
        + "}}"
    )
    (tmp_path / "First.json.tmp").write_text(
        json.dumps({"metadata": {"store_version": 1}, "data": {"value": 41}})
    )
    instance = engine(tmp_path, policy=policy)
    instance.boot()
    assert values(instance) == {"value": 41}
    assert not (tmp_path / "First.json").exists()
    assert not (tmp_path / "First.json.tmp").exists()
    restart = engine(tmp_path)
    restart.boot()
    assert values(restart) == {"value": 41}
