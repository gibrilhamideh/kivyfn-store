from __future__ import annotations

import pytest

from kivyfn.store.client.future import RequestFutures
from kivyfn.store.error import StoreConnectionError
from kivyfn.store.protocol.message.requests import (
    HistoryRequest,
    QueryRequest,
    TransactionRequest,
)
from kivyfn.store.protocol.message.responses import (
    QueryResponse,
    TransactionResponse,
)
from kivyfn.store.query import Query, QueryRow
from kivyfn.store.schema import ManySchema


class FutureRows(ManySchema):
    pass


@pytest.mark.asyncio
async def test_cancelled_waiter_keeps_mutation_pending_until_reply() -> None:
    futures = RequestFutures()
    request = TransactionRequest("cancelled", ())
    waiter = futures.create_transaction(request)
    waiter.cancel()

    assert futures.has_pending_transactions
    with pytest.raises(RuntimeError, match="already exists"):
        futures.create_transaction(request)

    other_request = TransactionRequest("other", ())
    other = futures.create_transaction(other_request)
    futures.resolve(request, TransactionResponse(request.transaction_id))
    assert waiter.cancelled()
    assert futures.has_pending_transactions
    assert not other.done()

    reply = TransactionResponse(other_request.transaction_id)
    futures.resolve(other_request, reply)
    assert await other is reply
    assert not futures.has_pending_transactions
    assert futures.empty


@pytest.mark.asyncio
async def test_equal_query_requests_have_independent_waiters() -> None:
    futures = RequestFutures()
    first_request = QueryRequest(Query(FutureRows))
    second_request = QueryRequest(first_request.query)
    assert first_request == second_request
    first = futures.create_query(first_request)
    second = futures.create_query(second_request)

    second_reply = QueryResponse((QueryRow("second", {}),))
    futures.resolve(second_request, second_reply)
    assert await second is second_reply
    assert not first.done()

    first_reply = QueryResponse((QueryRow("first", {}),))
    futures.resolve(first_request, first_reply)
    assert await first is first_reply
    assert futures.empty


@pytest.mark.asyncio
async def test_disconnect_settles_and_forgets_all_request_kinds() -> None:
    futures = RequestFutures()
    transaction = TransactionRequest("pending", ())
    query = QueryRequest(Query(FutureRows))
    history = HistoryRequest()
    mutation_waiter = futures.create_transaction(transaction)
    query_waiter = futures.create_query(query)
    history_waiter = futures.create_history(history)
    query_waiter.cancel()
    error = StoreConnectionError("Connection lost")

    futures.fail_all(error)
    assert futures.empty
    assert not futures.has_pending_transactions
    assert query_waiter.cancelled()
    assert mutation_waiter.exception() is error
    assert history_waiter.exception() is error

    # A late reply must neither revive old work nor resettle a failed waiter.
    futures.resolve(
        transaction, TransactionResponse(transaction.transaction_id)
    )
    assert mutation_waiter.exception() is error
    futures.fail_all(error)
    assert futures.empty
