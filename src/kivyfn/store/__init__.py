# src/kivyfn/store/__init__.py
from typing import TYPE_CHECKING, Any

from kivyfn.store.client.client import StoreClient
from kivyfn.store.host.host import StoreHost
from kivyfn.store.query import Query, QueryResult, QueryRow, field
from kivyfn.store.schema import ManySchema, Schema, SingleSchema
from kivyfn.store.store import Store
from kivyfn.store.address import Address

if TYPE_CHECKING:
    from kivyfn.store.state.template import StateTemplate, state_template

__all__ = [
    "ManySchema",
    "Query",
    "QueryResult",
    "QueryRow",
    "Schema",
    "SingleSchema",
    "StateTemplate",
    "Store",
    "StoreClient",
    "StoreHost",
    "field",
    "state_template",
]


def __getattr__(name: str) -> Any:
    # Store/server imports should not load the client-side reactive classes.
    if name in {"StateTemplate", "state_template"}:
        from kivyfn.store.state import template

        return getattr(template, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
