# src/kivyfn/store/host/request.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, assert_never, cast

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
from kivyfn.store.events import InsertEvent, SetEvent, SetRowEvent, StoreCommit
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    InsertRequest,
    MutationRequest,
    OperationRequest,
    QueryRequest,
    QuerySubscriptionRequest,
    RemoveRequest,
    Request,
    RequestType,
    SetRequest,
    SetRowRequest,
    SignalRequest,
    SignalRowRequest,
    SingleSubscriptionRequest,
    TransactionRequest,
    UnsubscribeRequest,
)
from kivyfn.store.protocol.message.responses import (
    FailureResponse,
    InsertResponse,
    OperationResponse,
    QueryResponse,
    RemoveResponse,
    Response,
    ResponseValue,
    SetResponse,
    SetRowResponse,
    TransactionResponse,
    UnsubscribeResponse,
)

if TYPE_CHECKING:
    from kivyfn.store.host.subscription import HostSubscriptions
    from kivyfn.store.store import Store


logger = get_logger(__name__)


class HostRequests:
    """Route decoded requests and construct their typed responses.

    The runtime holds Store.synchronized() while dispatching a batch. Each
    OperationRequest or TransactionRequest makes one Store.execute() call.
    Store owns instruction building, execution, and commit publication.
    """

    def __init__(self, store: Store, subscriptions: HostSubscriptions) -> None:
        self._store = store
        self._subscriptions = subscriptions

    def handle(self, client_id: str, request: Request) -> Response:
        return Response(
            tuple(
                self._handle_request(client_id, item) for item in request.items
            )
        )

    def _handle_request(
        self, client_id: str, request: RequestType
    ) -> ResponseValue:
        try:
            match request:
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
                        request_id=request.request_id,
                        subscription_id=request.subscription_id,
                    )
                case QueryRequest():
                    return QueryResponse(
                        request_id=request.request_id,
                        rows=self._store.query(request.query),
                    )
                case OperationRequest():
                    return self._operation(request)
                case TransactionRequest():
                    commit = self._execute(
                        request.mutations, request.operation_id
                    )
                    return TransactionResponse(
                        request_id=request.request_id,
                        operation_id=request.operation_id,
                        revision=cast(int, commit.revision),
                        commit=commit,
                    )
                case _:
                    assert_never(request)
        except StoreOperationError as error:
            failure = error.failure()
        except StoreStateError as error:
            failure = StoreFailure(
                code=StoreErrorCode.STORE_STATE_ERROR,
                category=StoreErrorCategory.STATE,
                status=StoreErrorStatus.UNAVAILABLE,
                message=str(error),
            )
        except (ValueError, TypeError) as error:
            failure = InvalidOperationError(str(error)).failure()
        except Exception:
            logger.exception("Store request %s failed", request.request_id)
            failure = StoreFailure(
                code=StoreErrorCode.INTERNAL_ERROR,
                category=StoreErrorCategory.INTERNAL,
                status=StoreErrorStatus.INTERNAL,
                message="Store request failed unexpectedly",
            )

        return FailureResponse(request_id=request.request_id, failure=failure)

    def _execute(
        self, mutations: tuple[MutationRequest, ...], operation_id: str
    ) -> StoreCommit:
        requests: list[
            SetRequest | SetRowRequest | InsertRequest | RemoveRequest
        ] = []

        for mutation in mutations:
            match mutation:
                case (
                    SetRequest()
                    | SetRowRequest()
                    | InsertRequest()
                    | RemoveRequest()
                ):
                    requests.append(mutation)
                case SignalRequest() | SignalRowRequest():
                    raise InvalidOperationError(
                        "Signals are not connected yet"
                    )
                case _:
                    assert_never(mutation)

        result = self._store.execute(
            tuple(requests), operation_id=operation_id
        )

        if not result.success:
            raise cast(StoreOperationError, result.error)

        return cast(StoreCommit, result.commit)

    def _operation(self, request: OperationRequest) -> OperationResponse:
        mutation = request.mutation
        commit = self._execute((mutation,), request.operation_id)
        revision = cast(int, commit.revision)

        match mutation:
            case SetRequest():
                return SetResponse(
                    request_id=request.request_id,
                    operation_id=request.operation_id,
                    revision=revision,
                    values=_operation_values(mutation, commit),
                )
            case SetRowRequest():
                return SetRowResponse(
                    request_id=request.request_id,
                    operation_id=request.operation_id,
                    revision=revision,
                    row_id=mutation.row_id,
                    values=_operation_values(mutation, commit),
                )
            case InsertRequest():
                return InsertResponse(
                    request_id=request.request_id,
                    operation_id=request.operation_id,
                    revision=revision,
                    row_id=mutation.row_id,
                    values=_operation_values(mutation, commit),
                )
            case RemoveRequest():
                return RemoveResponse(
                    request_id=request.request_id,
                    operation_id=request.operation_id,
                    revision=revision,
                    row_id=mutation.row_id,
                )
            case SignalRequest() | SignalRowRequest():
                raise InvalidOperationError("Signals are not connected yet")
            case _:
                assert_never(mutation)


def _operation_values(
    mutation: SetRequest | SetRowRequest | InsertRequest, commit: StoreCommit
) -> tuple[tuple[str, Any], ...]:
    """Acknowledge unchanged fields and the commit's canonical changes.

    Fields omitted from events were no-ops. Event values also include defaults
    and trigger effects. Reading Store again could observe a later revision.
    """
    row_id = None if isinstance(mutation, SetRequest) else mutation.row_id
    values = dict(mutation.values)

    for event in commit.events:
        if event.schema != mutation.schema:
            continue

        if isinstance(event, SetEvent) and row_id is None:
            values[event.key] = event.new
        elif isinstance(event, SetRowEvent) and event.id == row_id:
            values[event.key] = event.new
        elif isinstance(event, InsertEvent) and event.id == row_id:
            values = dict(event.new)

    if isinstance(mutation, InsertRequest):
        return tuple(values.items())

    return tuple((key, values[key]) for key, _ in mutation.values)
