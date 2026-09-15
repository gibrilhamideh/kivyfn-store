from __future__ import annotations

from typing import Any

import pytest

from kivyfn.store.address import Address
from kivyfn.store.error import InvalidOperationError
from kivyfn.store.node import Node
from kivyfn.store.schema import ManySchema, StringField


class IdentifiedRows(ManySchema):
    label = StringField(default="")
    uniques = [("label",)]


@pytest.mark.parametrize("row_id", ["", None, 0, True])
def test_invalid_insert_identity_cannot_change_values_or_indexes(
    row_id: Any,
) -> None:
    node = Node(IdentifiedRows(), Address())
    node.build({"existing": {"label": "retained"}})

    with pytest.raises(InvalidOperationError, match="non-empty string"):
        node.insert(row_id, {"label": "available"})

    assert node.snapshot() == {"existing": {"label": "retained"}}
    assert node.lookup_one("label", "available") is None
    node.insert("valid", {"label": "available"})
    assert node.lookup_one("label", "available") == "valid"


def test_build_rejects_empty_identity_and_recovers_only_invalid_row() -> None:
    node = Node(IdentifiedRows(), Address())
    values = {"": {"label": "invalid"}, "valid": {"label": "retained"}}
    with pytest.raises(InvalidOperationError, match="non-empty string"):
        node.build(values)

    node.build(values, recover=True)
    assert node.snapshot() == {"valid": {"label": "retained"}}
    assert node.lookup_one("label", "invalid") is None
    assert node.lookup_one("label", "retained") == "valid"


def test_build_preserves_existing_numeric_key_normalization() -> None:
    node = Node(IdentifiedRows(), Address())
    node.build({0: {"label": "zero"}})
    assert node.snapshot() == {"0": {"label": "zero"}}
