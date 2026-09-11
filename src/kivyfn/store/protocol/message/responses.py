# src/kivyfn/store/protocol/message/responses.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from kivyfn.store.error import StoreFailure
from kivyfn.store.events import StoreCommit
from kivyfn.store.query import QueryResult
from kivyfn.store.query.shape import QueryShape


@dataclass(frozen=True, slots=True)
class ResponseItem:
    """Base type for one item returned by a StoreHost."""

    request_id: str


@dataclass(frozen=True, slots=True)
class SetResponse(ResponseItem):
    """Successful SET result."""

    operation_id: str
    revision: int
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SetRowResponse(ResponseItem):
    """Successful SETROW result."""

    operation_id: str
    revision: int
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class InsertResponse(ResponseItem):
    """Successful INSERT result."""

    operation_id: str
    revision: int
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RemoveResponse(ResponseItem):
    """Successful REMOVE result."""

    operation_id: str
    revision: int
    row_id: str


@dataclass(frozen=True, slots=True)
class SignalResponse(ResponseItem):
    """Successful signal result."""

    operation_id: str
    revision: int


OperationResponse: TypeAlias = (
    SetResponse
    | SetRowResponse
    | InsertResponse
    | RemoveResponse
    | SignalResponse
)


@dataclass(frozen=True, slots=True)
class TransactionResponse(ResponseItem):
    """Successful atomic State transaction result."""

    operation_id: str
    revision: int
    commit: StoreCommit | None


@dataclass(frozen=True, slots=True)
class QueryResponse(ResponseItem):
    """Successful one-shot Query result."""

    rows: QueryResult


@dataclass(frozen=True, slots=True)
class SingleSubscriptionResponse(ResponseItem):
    """Initial hydration for one SingleSchema subscription."""

    subscription_id: str
    store_instance_id: str
    revision: int
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CollectionSubscriptionResponse(ResponseItem):
    """Initial hydration for one ManySchema subscription."""

    subscription_id: str
    store_instance_id: str
    revision: int
    rows: QueryResult


@dataclass(frozen=True, slots=True)
class QuerySubscriptionResponse(ResponseItem):
    """Initial hydration for one live Query subscription."""

    subscription_id: str
    store_instance_id: str
    revision: int
    shape: QueryShape
    rows: QueryResult


@dataclass(frozen=True, slots=True)
class UnsubscribeResponse(ResponseItem):
    """Confirm the subscription is no longer registered for this client.

    Releasing an already absent subscription uses the same acknowledgment.
    """

    subscription_id: str


@dataclass(frozen=True, slots=True)
class FailureResponse(ResponseItem):
    """Failure of one request item."""

    failure: StoreFailure


ResponseValue: TypeAlias = (
    SetResponse
    | SetRowResponse
    | InsertResponse
    | RemoveResponse
    | SignalResponse
    | TransactionResponse
    | QueryResponse
    | SingleSubscriptionResponse
    | CollectionSubscriptionResponse
    | QuerySubscriptionResponse
    | UnsubscribeResponse
    | FailureResponse
)


@dataclass(frozen=True, slots=True)
class Response:
    """Ordered response items returned in one transport response."""

    items: tuple[ResponseValue, ...]
