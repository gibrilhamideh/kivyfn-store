# src/kivyfn/store/protocol/message/responses.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from kivyfn.store.error import StoreFailure
from kivyfn.store.query import QueryResult


@dataclass(frozen=True, slots=True)
class TransactionResponse:
    """Confirm one successfully committed transaction."""

    transaction_id: str


@dataclass(frozen=True, slots=True)
class TransactionFailureResponse:
    """Reject one transaction."""

    transaction_id: str
    failure: StoreFailure


@dataclass(frozen=True, slots=True)
class QueryResponse:
    """Result of one one-shot query."""

    rows: QueryResult


@dataclass(frozen=True, slots=True)
class SingleSubscriptionResponse:
    """Register one SingleSchema subscription."""

    subscription_id: str
    store_instance_id: str
    revision: int
    values: tuple[tuple[str, Any], ...] | None


@dataclass(frozen=True, slots=True)
class CollectionSubscriptionResponse:
    """Register one ManySchema subscription."""

    subscription_id: str
    store_instance_id: str
    revision: int
    rows: QueryResult | None


@dataclass(frozen=True, slots=True)
class QuerySubscriptionResponse:
    """Register one live Query subscription."""

    subscription_id: str
    store_instance_id: str
    revisions: dict[str, int]
    rows: QueryResult | None


@dataclass(frozen=True, slots=True)
class UnsubscribeResponse:
    """Confirm removal of one Host subscription."""

    subscription_id: str


@dataclass(frozen=True, slots=True)
class FailureResponse:
    """Failure of a non-transaction request."""

    failure: StoreFailure


SubscriptionResponse: TypeAlias = (
    SingleSubscriptionResponse
    | CollectionSubscriptionResponse
    | QuerySubscriptionResponse
)


ResponseItem: TypeAlias = (
    TransactionResponse
    | TransactionFailureResponse
    | QueryResponse
    | SubscriptionResponse
    | UnsubscribeResponse
    | FailureResponse
)


@dataclass(frozen=True, slots=True)
class ResponseBatch:
    """One ordered batch of Store responses."""

    items: tuple[ResponseItem, ...]
