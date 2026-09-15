from __future__ import annotations

from pathlib import Path
from typing import Any

from kivyfn.store.builder import Branch, Builder
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    SetRequestAction,
    SetRowRequestAction,
)
from kivyfn.store.schema import Schema, SingleSchema
from kivyfn.store.store import Store


class EmptyRoot(SingleSchema):
    pass


def make_store(
    path: Path, *schemas: type[Schema], root_schema: type[Schema] = EmptyRoot
) -> Store:
    tree = Branch(root_schema)
    for schema in schemas:
        tree.child(schema.__name__, Branch(schema))
    root, config = Builder().tree(tree).storage_path(path).build()
    store = Store(root, config)
    store.boot()
    return store


def insert(schema: type[Schema], row_id: str, **values: Any):
    return InsertRequestAction(schema.__name__, row_id, tuple(values.items()))


def setrow(schema: type[Schema], row_id: str, **values: Any):
    return SetRowRequestAction(schema.__name__, row_id, tuple(values.items()))


def setvalue(schema: type[Schema], **values: Any):
    return SetRequestAction(schema.__name__, tuple(values.items()))
