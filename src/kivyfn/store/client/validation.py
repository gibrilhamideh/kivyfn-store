"""Match decoded replies to requests before any batch results are applied."""

from __future__ import annotations

from typing import TypeAlias, cast

from kivyfn.store.error import StoreProtocolError
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    HistoryRequest,
    QueryRequest,
    QuerySubscriptionRequest,
    RedoRequest,
    RequestBatch,
    RequestItem,
    SingleSubscriptionRequest,
    TransactionRequest,
    UndoRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    FailureResponse,
    HistoryResponse,
    QueryResponse,
    QuerySubscriptionResponse,
    ResponseBatch,
    ResponseItem,
    SingleSubscriptionResponse,
    TransactionFailureResponse,
    TransactionResponse,
    UnsubscribeResponse,
)

ResponsePairs: TypeAlias = tuple[tuple[RequestItem, ResponseItem], ...]

_TRANSACTION_REPLIES = (TransactionResponse, TransactionFailureResponse)
_RESPONSE_TYPES: dict[type, tuple[type, ...]] = {
    TransactionRequest: _TRANSACTION_REPLIES,
    UndoRequest: _TRANSACTION_REPLIES,
    RedoRequest: _TRANSACTION_REPLIES,
    HistoryRequest: (HistoryResponse, FailureResponse),
    QueryRequest: (QueryResponse, FailureResponse),
    SingleSubscriptionRequest: (SingleSubscriptionResponse, FailureResponse),
    CollectionSubscriptionRequest: (
        CollectionSubscriptionResponse,
        FailureResponse,
    ),
    QuerySubscriptionRequest: (QuerySubscriptionResponse, FailureResponse),
    UnsubscribeRequest: (UnsubscribeResponse, FailureResponse),
}


def validate_response_batch(
    request: RequestBatch, response: ResponseBatch
) -> ResponsePairs:
    """Check every pair before returning replies for delivery or completion.

    The codec validates individual messages. This boundary checks that the
    messages answer the requests sent, including their order-dependent count.
    """
    if len(request.items) != len(response.items):
        raise StoreProtocolError("Response batch does not match requests")
    pairs = tuple(zip(request.items, response.items))
    for item, reply in pairs:
        validate_response(item, reply)
    return pairs


def validate_response(request: RequestItem, response: ResponseItem) -> None:
    expected = _RESPONSE_TYPES.get(type(request))
    if expected is None:
        raise StoreProtocolError("Unsupported request type")
    if not isinstance(response, expected):
        raise StoreProtocolError(
            f"Invalid response for {type(request).__name__}"
        )
    if isinstance(request, (TransactionRequest, UndoRequest, RedoRequest)):
        transaction_reply = cast(
            TransactionResponse | TransactionFailureResponse, response
        )
        if transaction_reply.transaction_id != request.transaction_id:
            raise StoreProtocolError("Transaction response ID mismatch")
    elif isinstance(request, UnsubscribeRequest) and isinstance(
        response, UnsubscribeResponse
    ):
        if response.subscription_id != request.subscription_id:
            raise StoreProtocolError("Unsubscribe response ID mismatch")
