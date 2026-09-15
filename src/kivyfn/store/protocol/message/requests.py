# src/kivyfn/store/protocol/message/requests.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from kivyfn.store.query import Query

if TYPE_CHECKING:
    from kivyfn.store.store.history import HistoryPosition


@dataclass(frozen=True, slots=True)
class SetRequestAction:
    schema: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SetRowRequestAction:
    schema: str
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class InsertRequestAction:
    schema: str
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RemoveRequestAction:
    schema: str
    row_id: str


@dataclass(frozen=True, slots=True)
class SignalRequestAction:
    schema: str
    signal: str
    data: Any = None


@dataclass(frozen=True, slots=True)
class SignalRowRequestAction:
    schema: str
    row_id: str
    signal: str
    data: Any = None


RequestAction: TypeAlias = (
    SetRequestAction
    | SetRowRequestAction
    | InsertRequestAction
    | RemoveRequestAction
    | SignalRequestAction
    | SignalRowRequestAction
)


@dataclass(frozen=True, slots=True)
class TransactionRequest:
    """One atomic Store transaction."""

    transaction_id: str
    actions: tuple[RequestAction, ...]
    history_group: str | None = None
    history_label: str | None = None
    expected_store_instance_id: str | None = None
    expected_revisions: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True, slots=True)
class UndoRequest:
    transaction_id: str
    expected_position: HistoryPosition


@dataclass(frozen=True, slots=True)
class RedoRequest:
    transaction_id: str
    expected_position: HistoryPosition


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """One one-shot Store query."""

    query: Query[Any]


@dataclass(frozen=True, slots=True)
class SingleSubscriptionRequest:
    """Subscribe to one SingleSchema."""

    schema: str
    store_instance_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionSubscriptionRequest:
    """Subscribe to one ManySchema collection."""

    schema: str
    store_instance_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class QuerySubscriptionRequest:
    """Subscribe to one live Query result."""

    query: Query[Any]
    store_instance_id: str | None = None
    revisions: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class UnsubscribeRequest:
    """Remove one active Host subscription."""

    subscription_id: str


SubscriptionRequest: TypeAlias = (
    SingleSubscriptionRequest
    | CollectionSubscriptionRequest
    | QuerySubscriptionRequest
)


RequestItem: TypeAlias = (
    TransactionRequest
    | UndoRequest
    | RedoRequest
    | HistoryRequest
    | QueryRequest
    | SubscriptionRequest
    | UnsubscribeRequest
)


@dataclass(frozen=True, slots=True)
class RequestBatch:
    """One ordered batch of Store requests."""

    items: tuple[RequestItem, ...]
