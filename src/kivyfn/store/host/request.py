# src/kivyfn/store/host/request.py
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from typing import TYPE_CHECKING, assert_never

from kivyfn.logging import get_logger

from kivyfn.store.error import (
    InvalidOperationError,
    StoreErrorCategory,
    StoreErrorCode,
    StoreErrorStatus,
    StoreFailure,
    StoreOperationError,
    StoreStateError,
)
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
    FailureResponse,
    HistoryResponse,
    QueryResponse,
    ResponseBatch,
    ResponseItem,
    TransactionFailureResponse,
    TransactionResponse,
    UnsubscribeResponse,
)

if TYPE_CHECKING:
    from kivyfn.store.host.subscription import HostSubscriptions
    from kivyfn.store.store import Store


logger = get_logger(__name__)


class HostRequests:
    """Route request batches into Store and subscription operations."""

    def __init__(
        self,
        store: Store,
        subscriptions: HostSubscriptions,
        *,
        max_completed: int = 1000,
    ) -> None:
        if max_completed < 1:
            raise ValueError("max_completed must be positive")
        self._max_completed = max_completed
        self._completed: OrderedDict[str, tuple[RequestItem, ResponseItem]] = (
            OrderedDict()
        )
        self._store = store
        self._subscriptions = subscriptions

    def handle(self, client_id: str, batch: RequestBatch) -> ResponseBatch:
        return ResponseBatch(
            items=tuple(
                self._handle(client_id, request) for request in batch.items
            )
        )

    def _handle(self, client_id: str, request: RequestItem) -> ResponseItem:
        if not isinstance(
            request, (TransactionRequest, UndoRequest, RedoRequest)
        ):
            return self._execute_request(client_id, request)
        cached = self._completed.get(request.transaction_id)
        if cached is not None:
            previous, response = cached
            if previous != request:
                error = InvalidOperationError(
                    "Transaction ID was already used with different work"
                )
                return TransactionFailureResponse(
                    request.transaction_id, error.failure()
                )
            self._completed.move_to_end(request.transaction_id)
            return deepcopy(response)
        response = self._execute_request(client_id, request)
        self._completed[request.transaction_id] = (
            deepcopy(request),
            deepcopy(response),
        )
        while len(self._completed) > self._max_completed:
            self._completed.popitem(last=False)
        return response

    def _execute_request(
        self, client_id: str, request: RequestItem
    ) -> ResponseItem:
        try:
            match request:
                case TransactionRequest():
                    return self._transaction(client_id, request)

                case HistoryRequest():
                    return HistoryResponse(
                        self._store.history(
                            limit=request.limit, offset=request.offset
                        )
                    )

                case UndoRequest() | RedoRequest():
                    navigate = (
                        self._store.undo
                        if isinstance(request, UndoRequest)
                        else self._store.redo
                    )
                    result = navigate(
                        transaction_id=request.transaction_id,
                        expected_position=request.expected_position,
                        origin_client_id=client_id,
                    )
                    if not result.success:
                        assert result.error is not None
                        raise result.error
                    return TransactionResponse(request.transaction_id)

                case QueryRequest():
                    return QueryResponse(rows=self._store.query(request.query))

                case (
                    SingleSubscriptionRequest()
                    | CollectionSubscriptionRequest()
                ):
                    return self._subscriptions.subscribe_schema(
                        client_id, request
                    )

                case QuerySubscriptionRequest():
                    return self._subscriptions.subscribe_query(
                        client_id, request
                    )

                case UnsubscribeRequest():
                    self._subscriptions.release(
                        client_id, request.subscription_id
                    )
                    return UnsubscribeResponse(
                        subscription_id=request.subscription_id
                    )

                case _:
                    assert_never(request)

        except Exception as error:
            failure = self._failure(request, error)

            if isinstance(
                request, (TransactionRequest, UndoRequest, RedoRequest)
            ):
                return TransactionFailureResponse(
                    transaction_id=request.transaction_id, failure=failure
                )

            return FailureResponse(failure=failure)

    def _transaction(
        self, client_id: str, request: TransactionRequest
    ) -> TransactionResponse:
        result = self._store.execute(
            request.actions,
            transaction_id=request.transaction_id,
            origin_client_id=client_id,
            history_group=request.history_group,
            history_label=request.history_label,
            expected_store_instance_id=request.expected_store_instance_id,
            expected_revisions=request.expected_revisions,
        )

        if not result.success:
            assert result.error is not None
            raise result.error

        return TransactionResponse(transaction_id=request.transaction_id)

    def _failure(self, request: RequestItem, error: Exception) -> StoreFailure:
        if isinstance(error, StoreOperationError):
            return error.failure()

        if isinstance(error, StoreStateError):
            return StoreFailure(
                code=StoreErrorCode.STORE_STATE_ERROR,
                category=StoreErrorCategory.STATE,
                status=StoreErrorStatus.UNAVAILABLE,
                message=str(error),
            )

        if isinstance(error, (ValueError, TypeError)):
            return InvalidOperationError(str(error)).failure()

        logger.exception(
            "Store request %s failed", type(request).__name__, exc_info=error
        )

        return StoreFailure(
            code=StoreErrorCode.INTERNAL_ERROR,
            category=StoreErrorCategory.INTERNAL,
            status=StoreErrorStatus.INTERNAL,
            message="Store request failed unexpectedly",
        )
