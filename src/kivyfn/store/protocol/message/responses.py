# src/kivyfn/store/protocol/message/responses.py
"""Typed response data; wire validation belongs to the response codec.

Successful writes confirm Store execution. They carry no canonical field
values or commits; subscription events apply those changes to State.
schema_revisions gives completion targets within store_instance_id. Host
includes requested schemas even for no-op writes, plus changed schemas from
triggers and policies. Client checks the relevant active subscriptions before
completing State operations; it must not wait for schemas it does not follow.
Receiving a response alone never advances a subscription's applied revision.

Subscription snapshots and revision metadata describe one consistent Store
view. None means reuse a complete confirmed cache whose Store instance and
revisions match. Empty tuples represent real empty snapshots. Client must
reject cache reuse if its matching confirmed data is no longer available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from kivyfn.store.error import StoreFailure
from kivyfn.store.query import QueryResult
from kivyfn.store.query.shape import QueryShape


@dataclass(frozen=True, slots=True)
class ResponseItem:
    """Base type for one item returned by a StoreHost."""

    request_id: str


@dataclass(frozen=True, slots=True)
class SetResponse(ResponseItem):
    """Confirm successful SET execution without returning field values."""

    operation_id: str
    store_instance_id: str
    schema_revisions: dict[str, int]


@dataclass(frozen=True, slots=True)
class SetRowResponse(ResponseItem):
    """Confirm successful SETROW execution for the identified row."""

    operation_id: str
    store_instance_id: str
    schema_revisions: dict[str, int]
    row_id: str


@dataclass(frozen=True, slots=True)
class InsertResponse(ResponseItem):
    """Confirm successful INSERT execution and the inserted row ID."""

    operation_id: str
    store_instance_id: str
    schema_revisions: dict[str, int]
    row_id: str


@dataclass(frozen=True, slots=True)
class RemoveResponse(ResponseItem):
    """Confirm successful REMOVE execution for the identified row."""

    operation_id: str
    store_instance_id: str
    schema_revisions: dict[str, int]
    row_id: str


@dataclass(frozen=True, slots=True)
class SignalResponse(ResponseItem):
    """Confirm successful signal dispatch in this Store instance.

    This does not claim that State changed or asynchronous handlers finished.
    Canonical changes caused by a handler arrive through normal Store events.
    """

    operation_id: str
    store_instance_id: str


OperationResponse: TypeAlias = (
    SetResponse
    | SetRowResponse
    | InsertResponse
    | RemoveResponse
    | SignalResponse
)


@dataclass(frozen=True, slots=True)
class TransactionResponse(ResponseItem):
    """Confirm successful atomic execution without embedding its commit.

    schema_revisions follows the same completion rules as individual writes.
    A successful transaction with no mutations can have an empty mapping.
    """

    operation_id: str
    store_instance_id: str
    schema_revisions: dict[str, int]


@dataclass(frozen=True, slots=True)
class QueryResponse(ResponseItem):
    """Successful one-shot Query result."""

    rows: QueryResult


@dataclass(frozen=True, slots=True)
class SingleSubscriptionResponse(ResponseItem):
    """Register a SingleSchema subscription and establish its schema revision.

    values=None confirms unchanged cached data. Otherwise values is the
    complete snapshot, including an empty tuple for a schema with no fields.
    revision belongs to this schema, not to the whole Store.
    """

    subscription_id: str
    store_instance_id: str
    revision: int
    values: tuple[tuple[str, Any], ...] | None


@dataclass(frozen=True, slots=True)
class CollectionSubscriptionResponse(ResponseItem):
    """Register a ManySchema subscription and establish its schema revision.

    rows=None confirms unchanged cached data. An empty tuple is a complete
    empty collection snapshot and must remove previously cached rows.
    revision belongs to this schema, not to the whole Store.
    """

    subscription_id: str
    store_instance_id: str
    revision: int
    rows: QueryResult | None


@dataclass(frozen=True, slots=True)
class QuerySubscriptionResponse(ResponseItem):
    """Register a live Query with its complete dependency revision mapping.

    schema_revisions includes the root schema and every joined dependency,
    including schemas still at revision zero. It describes this query's
    confirmed result, not a complete cache of those individual schemas.

    rows=None reuses a complete result for the same query, Store instance,
    and dependency revisions. An empty tuple replaces it with an empty
    result. The resolved shape is supplied for every registration.
    """

    subscription_id: str
    store_instance_id: str
    schema_revisions: dict[str, int]
    shape: QueryShape
    rows: QueryResult | None


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
