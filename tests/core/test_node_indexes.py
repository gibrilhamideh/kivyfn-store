from __future__ import annotations

from typing import Any

import pytest

from kivyfn.store.address import Address
from kivyfn.store.error import (
    IndexNotFoundError,
    InvalidOperationError,
    RowAlreadyExistsError,
    RowNotFoundError,
    UniqueConstraintError,
)
from kivyfn.store.node import Node
from kivyfn.store.schema import (
    DataField,
    ManySchema,
    NumericField,
    SingleSchema,
    StringField,
)


class Indexed(ManySchema):
    label = StringField(default="")
    value = NumericField(default=0)
    group = StringField(default="")
    payload = DataField[dict[str, Any]](default={"items": []})
    uniques = [("label",)]
    indexes = [("value",), ("group", "value")]


def make_node() -> Node:
    node = Node(Indexed(), Address("rows"))
    node.build(None)
    return node


def test_index_rejection_does_not_change_other_indexes() -> None:
    node = make_node()
    node.insert("a", node.build_row({"label": "a", "value": 1, "group": "g"}))
    node.insert("b", node.build_row({"label": "b", "value": 2, "group": "g"}))
    with pytest.raises(UniqueConstraintError):
        node.setrow("a", "label", "b")
    assert node.lookup_one("label", "a") == "a"
    assert node.lookup_all("value", 1) == frozenset({"a"})
    assert node.lookup_all(("group", "value"), ("g", 1)) == frozenset({"a"})
    assert node.snapshot("a", "label") == "a"
    node.setrow("a", "value", 2)
    assert node.lookup_all("value", 1) == frozenset()
    assert node.lookup_all("value", 2) == frozenset({"a", "b"})
    node.remove("a")
    assert node.lookup_all("value", 2) == frozenset({"b"})
    assert node.lookup_one("label", "a") is None


def test_duplicate_insert_does_not_pollute_indexes() -> None:
    node = make_node()
    node.insert("a", node.build_row({"label": "a"}))
    with pytest.raises(RowAlreadyExistsError):
        node.insert("a", node.build_row({"label": "b"}))
    assert node.lookup_one("label", "a") == "a"
    assert node.lookup_one("label", "b") is None
    with pytest.raises(UniqueConstraintError):
        node.insert("b", node.build_row({"label": "a", "value": 9}))
    assert not node.exists("b")
    assert node.lookup_all("value", 9) == frozenset()


def test_low_level_node_mutations_take_ownership() -> None:
    node = make_node()
    row: dict[str, Any] = node.build_row({"label": "a"})
    node.insert("a", row)
    row["payload"]["items"].append("caller")
    assert node.snapshot("a", "payload") == {"items": []}
    value: dict[str, Any] = {"items": [1]}
    old = node.setrow("a", "payload", value)
    old["items"].append("old")
    value["items"].append(2)
    assert node.snapshot("a", "payload") == {"items": [1]}
    removed = node.remove("a")
    removed["payload"]["items"].append("removed")
    assert node.snapshot("a") is None
    assert node.revision == 0


def test_single_node_set_takes_ownership() -> None:
    class Settings(SingleSchema):
        payload = DataField[dict[str, Any]](default={"items": []})

    node = Node(Settings(), Address())
    node.build(None)
    value: dict[str, Any] = {"items": [1]}
    old = node.set("payload", value)
    old["items"].append("old")
    value["items"].append(2)
    assert node.snapshot(key="payload") == {"items": [1]}
    with pytest.raises(InvalidOperationError):
        node.insert("a", {})
    with pytest.raises(InvalidOperationError):
        node.lookup_all("payload", None)


def test_missing_rows_indexes_and_wrong_node_kind_are_domain_errors() -> None:
    node = make_node()
    with pytest.raises(RowNotFoundError):
        node.setrow("missing", "value", 1)
    with pytest.raises(RowNotFoundError):
        node.remove("missing")
    with pytest.raises(IndexNotFoundError):
        node.lookup_all("unknown", 1)
    with pytest.raises(InvalidOperationError):
        node.set("value", 1)
    assert node.snapshot("missing") is None
    assert node.snapshot("missing", "value") is None


def test_prepare_isolated_until_install_and_supports_unique_swap() -> None:
    node = make_node()
    node.insert("a", node.build_row({"label": "a"}))
    node.insert("b", node.build_row({"label": "b"}))
    future = node.snapshot()
    future["a"]["label"] = "b"
    future["b"]["label"] = "a"
    state = node.prepare(future)
    assert node.lookup_one("label", "a") == "a"
    assert node.snapshot("a", "label") == "a"
    node.install(state)
    assert node.lookup_one("label", "a") == "b"
    assert node.snapshot("a", "label") == "b"


def test_node_tree_rejects_duplicate_children() -> None:
    root = make_node()
    child = make_node()
    root.add_child("child", child)
    with pytest.raises(ValueError, match="already exists"):
        root.add_child("child", child)
    assert root.children == {"child": child}
