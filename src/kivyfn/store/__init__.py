# src/kivyfn/store/__init__.py
from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.client.client import StoreClient
from kivyfn.store.host.host import StoreHost
from kivyfn.store.query import Query, QueryResult, QueryRow, field
from kivyfn.store.schema import ManySchema, Schema, SingleSchema
from kivyfn.store.store import Store

if TYPE_CHECKING:
    from kivyfn.store.builder import (
        Branch,
        Builder,
        HistoryConfig,
        HostConfig,
        Initializer,
        StoreConfig,
    )
    from kivyfn.store.devtools.config import DevToolsConfig
    from kivyfn.store.state.template import StateTemplate, state_template
    from kivyfn.store.store.history import (
        HistoryEntry,
        HistoryPage,
        HistoryPosition,
        HistoryStatus,
    )

__all__ = [
    "Address",
    "Branch",
    "Builder",
    "DevToolsConfig",
    "HistoryConfig",
    "HistoryEntry",
    "HistoryPage",
    "HistoryPosition",
    "HistoryStatus",
    "HostConfig",
    "Initializer",
    "ManySchema",
    "Query",
    "QueryResult",
    "QueryRow",
    "Schema",
    "SingleSchema",
    "StateTemplate",
    "Store",
    "StoreClient",
    "StoreConfig",
    "StoreHost",
    "field",
    "state_template",
]


def __getattr__(name: str) -> Any:
    # Store/server imports should not load the client-side reactive classes.
    if name in {"StateTemplate", "state_template"}:
        from kivyfn.store.state import template

        return getattr(template, name)
    if name in {
        "Branch",
        "Builder",
        "HistoryConfig",
        "HostConfig",
        "Initializer",
        "StoreConfig",
    }:
        from kivyfn.store import builder

        return getattr(builder, name)
    if name == "DevToolsConfig":
        from kivyfn.store.devtools.config import DevToolsConfig

        return DevToolsConfig
    if name in {
        "HistoryEntry",
        "HistoryPage",
        "HistoryPosition",
        "HistoryStatus",
    }:
        from kivyfn.store.store import history

        return getattr(history, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
