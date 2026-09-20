from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.builder.branch import Branch
from kivyfn.store.builder.builder import Builder
from kivyfn.store.builder.config import (
    DevToolsConfig,
    HistoryConfig,
    HostConfig,
    StoreConfig,
)
from kivyfn.store.builder.initializer import Initializer
from kivyfn.store.store.store import Store
from kivyfn.store.address import Address
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    SetRequestAction,
)
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.fields import NumericField


class Settings(SingleSchema):
    track_history = True
    value = NumericField(default=0)


class Rows(ManySchema):
    value = NumericField(default=0)
    uniques = [("value",)]


class Seed(Initializer):
    def __init__(self, value: int) -> None:
        self.value = value

    def run(self, data: dict[str, Any]) -> dict[str, Any]:
        data["Settings"] = {"value": self.value}
        return data


def test_builder_contains_complete_detached_configuration(
    tmp_path: Path,
) -> None:
    tree = Branch(Settings).child("rows", Branch(Rows))
    initializer = Seed(4)
    history = HistoryConfig(max_entries=12)
    host = HostConfig("http://127.0.0.1:0", devtools=True)
    config = (
        Builder()
        .tree(tree)
        .storage_path(tmp_path)
        .initializer(initializer)
        .history(history)
        .host(host)
        .build()
    )
    assert isinstance(config, StoreConfig)
    assert config.root is not tree
    assert config.root.children["rows"] is not tree.children["rows"]
    assert config.initializer is initializer
    assert config.history is history
    assert config.host is host
    tree.child("late", Branch(Settings))
    assert set(config.root.children) == {"rows"}


def test_same_configuration_owns_separate_nodes_indexes_and_history(
    tmp_path: Path,
) -> None:
    config = (
        Builder()
        .tree(Branch(Settings).child("rows", Branch(Rows)))
        .storage_path(tmp_path)
        .history(HistoryConfig())
        .host(HostConfig("http://127.0.0.1:0", devtools=True))
        .build()
    )
    first = Store(config)
    second = Store(config)
    assert first.config is config
    assert second.config is config
    first.boot()
    second.boot()
    result = first.execute(
        (
            SetRequestAction("Settings", (("value", 7),)),
            InsertRequestAction("Rows", "first", (("value", 2),)),
        ),
        transaction_id="first",
    )
    assert result.success
    assert first.snapshot_by_schema("Settings") == {"value": 7}
    assert second.snapshot_by_schema("Settings") == {"value": 0}
    assert second.snapshot_by_schema("Rows") == {}
    assert len(first.history().entries) == 1
    assert second.history().entries == ()
    assert second.schema_revisions == {"Settings": 0, "Rows": 0}
    assert second.execute(
        (InsertRequestAction("Rows", "second", (("value", 2),)),),
        transaction_id="second",
    ).success
    assert set(first.snapshot_by_schema("Rows")) == {"first"}
    assert set(second.snapshot_by_schema("Rows")) == {"second"}
    first_node = first.get_node_by_address(Address("rows"))
    second_node = second.get_node_by_address(Address("rows"))
    assert first_node is not second_node
    assert first_node._indexes is not second_node._indexes
    assert first._host_owner is None
    assert second._host_owner is None
    first.shutdown()
    second.shutdown()


def test_configuration_roundtrip_preserves_initializer_and_host_settings(
    tmp_path: Path,
) -> None:
    config = (
        Builder()
        .tree(Branch(Settings))
        .storage_path(tmp_path)
        .initializer(Seed(9))
        .history(HistoryConfig(max_entries=20))
        .host(HostConfig("direct://roundtrip", DevToolsConfig(port=0)))
        .build()
    )
    restored = pickle.loads(pickle.dumps(config))
    assert restored.host == config.host
    assert restored.history == config.history
    store = Store(restored)
    store.boot()
    assert store.snapshot_by_schema("Settings") == {"value": 9}
    assert store.history().entries == ()
    store.shutdown()


def test_configuration_reference_is_read_only(tmp_path: Path) -> None:
    config = Builder().tree(Branch(Settings)).storage_path(tmp_path).build()
    store = Store(config)
    with pytest.raises(AttributeError):
        setattr(store, "config", config)


@pytest.mark.parametrize("setting", ["initializer", "history", "host"])
def test_builder_and_direct_config_reject_invalid_settings_once(
    setting: str,
) -> None:
    invalid: Any = object()
    with pytest.raises(TypeError, match=setting):
        StoreConfig(root=Branch(Settings), **{setting: invalid})
    builder = Builder().tree(Branch(Settings))
    getattr(builder, setting)(invalid)
    with pytest.raises(TypeError, match=setting):
        builder.build()


@pytest.mark.parametrize("root", ["Settings", Settings, Settings(), 5])
def test_config_rejects_runtime_or_invalid_tree(root: Any) -> None:
    with pytest.raises(TypeError, match="Branch"):
        StoreConfig(root=root)
    with pytest.raises(TypeError, match="Branch"):
        Builder().tree(root).build()


def test_config_validates_cycles_for_direct_construction() -> None:
    root = Branch(Settings)
    root.child("loop", root)
    with pytest.raises(RuntimeError, match="Cycle"):
        StoreConfig(root=root)


@pytest.mark.parametrize("devtools", [None, 1, "true", {}])
def test_host_config_rejects_invalid_devtools(devtools: Any) -> None:
    with pytest.raises(TypeError, match="devtools"):
        HostConfig("direct://invalid", devtools=devtools)


def test_host_config_leaves_endpoint_parsing_to_transport() -> None:
    with pytest.raises(TypeError, match="endpoint"):
        HostConfig(3)  # type: ignore[arg-type]
    config = HostConfig("custom://unresolved")
    assert config.endpoint == "custom://unresolved"
    assert config.devtools is False
