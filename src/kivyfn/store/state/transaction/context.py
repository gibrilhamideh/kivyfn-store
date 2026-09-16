# src/kivyfn/store/state/transaction/context.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.state import State
from kivyfn.store.state.transaction.transaction import StateTransaction


@dataclass(slots=True)
class TransactionContext:
    """Lifecycle participants for one submitted or starting transaction.

    Row writes and signals notify States; inserts and removals notify their
    collections. Overlay cleanup also covers inserted rows, even when those
    rows are not lifecycle participants.
    """

    transaction: StateTransaction
    states: set[State[Any]]
    collections: set[CollectionState[Any]]
