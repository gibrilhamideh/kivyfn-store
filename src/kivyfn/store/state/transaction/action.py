# src/kivyfn/store/state/transaction/action.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.state import State


@dataclass(slots=True)
class SetAction:
    """Set fields on one SingleSchema State."""

    state: State[Any]
    values: dict[str, Any]


@dataclass(slots=True)
class SetRowAction:
    """Set fields on one existing ManySchema row."""

    state: State[Any]
    values: dict[str, Any]


@dataclass(slots=True)
class InsertAction:
    """Insert one row into a CollectionState."""

    collection: CollectionState[Any]
    state: State[Any]
    values: dict[str, Any]


@dataclass(slots=True)
class RemoveAction:
    """Remove one row from a CollectionState."""

    collection: CollectionState[Any]
    state: State[Any]


@dataclass(slots=True)
class SignalAction:
    """Invoke one State signal."""

    state: State[Any]
    signal: str
    data: Any


TransactionAction: TypeAlias = (
    SetAction | SetRowAction | InsertAction | RemoveAction | SignalAction
)
