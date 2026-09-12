# src/kivyfn/store/protocol/message/requests.py
"""Typed requests; incoming payload validation belongs to the codec.

Subscription cache hints pair a Store instance ID with applied revisions.
Send them only while retaining a complete, valid confirmed snapshot for the
same subscription. Omit both fields to request a fresh snapshot, including
after an event application failure. Optimistic values are not cache data.

These hints allow unchanged snapshots to be omitted; they never request
replay or impose write preconditions. The host must register subscriptions,
compare revisions, and capture snapshots under one consistent boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from kivyfn.store.query import Query


@dataclass(frozen=True, slots=True)
class SetRequest:
    """SET fields on one SingleSchema record."""

    schema: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SetRowRequest:
    """SETROW fields on one ManySchema row."""

    schema: str
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class InsertRequest:
    """INSERT one ManySchema row."""

    schema: str
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RemoveRequest:
    """REMOVE one ManySchema row."""

    schema: str
    row_id: str


@dataclass(frozen=True, slots=True)
class SignalRequest:
    """Emit one signal on a SingleSchema State."""

    schema: str
    signal: str
    data: Any = None


@dataclass(frozen=True, slots=True)
class SignalRowRequest:
    """Emit one signal on a ManySchema row State."""

    schema: str
    row_id: str
    signal: str
    data: Any = None


MutationRequest: TypeAlias = (
    SetRequest
    | SetRowRequest
    | InsertRequest
    | RemoveRequest
    | SignalRequest
    | SignalRowRequest
)


@dataclass(frozen=True, slots=True)
class RequestItem:
    """Base type for one top-level Store request item."""

    request_id: str


@dataclass(frozen=True, slots=True)
class OperationRequest(RequestItem):
    """Execute one State mutation."""

    operation_id: str
    mutation: MutationRequest


@dataclass(frozen=True, slots=True)
class TransactionRequest(RequestItem):
    """Execute ordered State mutations atomically."""

    operation_id: str
    mutations: tuple[MutationRequest, ...]


@dataclass(frozen=True, slots=True)
class QueryRequest(RequestItem):
    """Execute one one-shot Query."""

    query: Query[Any]


@dataclass(frozen=True, slots=True)
class SingleSubscriptionRequest(RequestItem):
    """Subscribe to one SingleSchema, optionally reusing its cached data."""

    schema: str
    store_instance_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionSubscriptionRequest(RequestItem):
    """Subscribe to one ManySchema, optionally reusing its complete cache."""

    schema: str
    store_instance_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class QuerySubscriptionRequest(RequestItem):
    """Subscribe to one live Query, optionally reusing its complete result.

    Cache revisions must cover the root and every joined schema of the
    exact same query. The host resolves and checks those dependencies before
    deciding whether the cached result can replace a fresh snapshot.
    """

    query: Query[Any]
    store_instance_id: str | None = None
    schema_revisions: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class UnsubscribeRequest(RequestItem):
    """Release one subscription owned by the requesting client."""

    subscription_id: str


RequestType: TypeAlias = (
    OperationRequest
    | TransactionRequest
    | QueryRequest
    | SingleSubscriptionRequest
    | CollectionSubscriptionRequest
    | QuerySubscriptionRequest
    | UnsubscribeRequest
)


@dataclass(frozen=True, slots=True)
class Request:
    """Ordered request items sent in one transport request."""

    items: tuple[RequestType, ...]
