"""Create and complete the futures belonging to outgoing Store requests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeAlias, TypeVar, cast

from kivyfn.store.protocol.message.requests import (
    HistoryRequest,
    QueryRequest,
    RedoRequest,
    RequestItem,
    TransactionRequest,
    UndoRequest,
)
from kivyfn.store.protocol.message.responses import (
    FailureResponse,
    HistoryResponse,
    QueryResponse,
    ResponseItem,
    TransactionFailureResponse,
    TransactionResponse,
)

TransactionCommand: TypeAlias = TransactionRequest | UndoRequest | RedoRequest
TransactionReply: TypeAlias = TransactionResponse | TransactionFailureResponse
QueryReply: TypeAlias = QueryResponse | FailureResponse
HistoryReply: TypeAlias = HistoryResponse | FailureResponse
ReplyT = TypeVar("ReplyT")


class RequestFutures:
    """Own request waiters until a response or connection failure arrives.

    Cancelling a waiter does not remove its pending request: a mutation may
    still commit, and undo/redo must wait for its outcome. Connection readiness
    and StateTransaction lifecycle futures belong to their respective owners.
    """

    def __init__(self) -> None:
        self._transactions: dict[str, asyncio.Future[TransactionReply]] = {}
        self._queries: dict[int, asyncio.Future[QueryReply]] = {}
        self._history: dict[int, asyncio.Future[HistoryReply]] = {}
        self._handlers: dict[type, Callable[..., None]] = {
            TransactionRequest: self._resolve_transaction,
            UndoRequest: self._resolve_transaction,
            RedoRequest: self._resolve_transaction,
            QueryRequest: self._resolve_query,
            HistoryRequest: self._resolve_history,
        }

    @property
    def has_pending_transactions(self) -> bool:
        return bool(self._transactions)

    @property
    def empty(self) -> bool:
        return not (self._transactions or self._queries or self._history)

    def create_transaction(
        self, request: TransactionCommand
    ) -> asyncio.Future[TransactionReply]:
        transaction_id = request.transaction_id
        if transaction_id in self._transactions:
            raise RuntimeError(f"Transaction already exists: {transaction_id}")
        future: asyncio.Future[TransactionReply] = self._create()
        self._transactions[transaction_id] = future
        return future

    def create_query(
        self, request: QueryRequest
    ) -> asyncio.Future[QueryReply]:
        future: asyncio.Future[QueryReply] = self._create()
        self._queries[id(request)] = future
        return future

    def create_history(
        self, request: HistoryRequest
    ) -> asyncio.Future[HistoryReply]:
        future: asyncio.Future[HistoryReply] = self._create()
        self._history[id(request)] = future
        return future

    def resolve(self, request: RequestItem, response: ResponseItem) -> None:
        """Complete a validated reply after subscription updates are delivered.

        Subscription and unsubscribe replies have no future in this registry.
        Already completed or cancelled waiters are removed without resettling.
        """
        handler = self._handlers.get(type(request))
        if handler is not None:
            handler(request, response)

    def fail_all(self, error: Exception) -> None:
        """Forget outstanding requests and fail their unfinished waiters."""
        futures = (
            *self._transactions.values(),
            *self._queries.values(),
            *self._history.values(),
        )
        self._transactions.clear()
        self._queries.clear()
        self._history.clear()
        for future in futures:
            if not future.done():
                future.set_exception(error)

    def _resolve_transaction(
        self, request: TransactionCommand, response: ResponseItem
    ) -> None:
        future = self._transactions.pop(request.transaction_id, None)
        self._complete(future, cast(TransactionReply, response))

    def _resolve_query(
        self, request: QueryRequest, response: ResponseItem
    ) -> None:
        future = self._queries.pop(id(request), None)
        self._complete(future, cast(QueryReply, response))

    def _resolve_history(
        self, request: HistoryRequest, response: ResponseItem
    ) -> None:
        future = self._history.pop(id(request), None)
        self._complete(future, cast(HistoryReply, response))

    @staticmethod
    def _create() -> asyncio.Future[Any]:
        return asyncio.get_running_loop().create_future()

    @staticmethod
    def _complete(
        future: asyncio.Future[ReplyT] | None, reply: ReplyT
    ) -> None:
        if future is not None and not future.done():
            future.set_result(reply)
