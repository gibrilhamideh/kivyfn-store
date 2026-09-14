# src/kivyfn/store/host/request.py
from __future__ import annotations

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
    InsertRequestAction,
    QueryRequest,
    QuerySubscriptionRequest,
    RemoveRequestAction,
    RequestBatch,
    RequestItem,
    SetRequestAction,
    SetRowRequestAction,
    SignalRequestAction,
    SignalRowRequestAction,
    SingleSubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    FailureResponse,
    QueryResponse,
    ResponseBatch,
    ResponseItem,
    TransactionFailureResponse,
    TransactionResponse,
    UnsubscribeResponse,
)
from kivyfn.store.store.instructions import StoreRequestAction


if TYPE_CHECKING:
    from kivyfn.store.host.subscription import HostSubscriptions
    from kivyfn.store.store import Store


logger = get_logger(__name__)


class HostRequests:
    """Route request batches into Store and subscription operations."""

    def __init__(self, store: Store, subscriptions: HostSubscriptions) -> None:
        self._store = store
        self._subscriptions = subscriptions

    def handle(self, client_id: str, batch: RequestBatch) -> ResponseBatch:
        return ResponseBatch(
            items=tuple(
                self._handle(client_id, request) for request in batch.items
            )
        )

    def _handle(self, client_id: str, request: RequestItem) -> ResponseItem:
        try:
            match request:
                case TransactionRequest():
                    return self._transaction(request)

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

            if isinstance(request, TransactionRequest):
                return TransactionFailureResponse(
                    transaction_id=request.transaction_id, failure=failure
                )

            return FailureResponse(failure=failure)

    def _transaction(self, request: TransactionRequest) -> TransactionResponse:
        actions = self._store_actions(request)

        result = self._store.execute(
            actions, transaction_id=request.transaction_id
        )

        if not result.success:
            assert result.error is not None
            raise result.error

        return TransactionResponse(transaction_id=request.transaction_id)

    def _store_actions(
        self, request: TransactionRequest
    ) -> tuple[StoreRequestAction, ...]:
        actions: list[StoreRequestAction] = []

        for action in request.actions:
            match action:
                case (
                    SetRequestAction()
                    | SetRowRequestAction()
                    | InsertRequestAction()
                    | RemoveRequestAction()
                    | SignalRequestAction()
                    | SignalRowRequestAction()
                ):
                    actions.append(action)

                case _:
                    assert_never(action)

        return tuple(actions)

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
