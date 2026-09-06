from __future__ import annotations

import asyncio
import inspect
import weakref

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, cast

from kivyfn.logging import get_logger
from kivyfn.store.query import Query, QueryChangeSet
from kivyfn.store.query.codec import query_to_payload
from kivyfn.store.query.event import (
    QueryResult,
    apply_query_change,
    copy_query_result,
    diff_query_results,
)
from kivyfn.store.query.sync import QuerySyncSnapshot
from kivyfn.store.remote.subscription import DispatchResult
from kivyfn.store.response import Response


logger = get_logger(__name__)

QueryCallback = Callable[[QueryChangeSet], None]
QuerySyncCallback = Callable[[bool, str], None]


@dataclass(frozen=True, slots=True)
class QuerySubscriptionSpec:
    query: Query

    def payload(self) -> dict[str, Any]:
        return {
            "query": query_to_payload(self.query),
        }


class RemoteQueryClient(Protocol):
    async def dispose_query_subscription(
        self,
        subscription: RemoteQuerySubscription,
    ) -> Response[bool] | None:
        ...

    async def query_sync_snapshot(
        self,
        subscription: RemoteQuerySubscription,
    ) -> Response[QuerySyncSnapshot]:
        ...


class _CallbackReference:
    __slots__ = (
        "_strong",
        "_weak",
    )

    def __init__(self, callback: QueryCallback) -> None:
        self._strong: QueryCallback | None = None
        self._weak: weakref.WeakMethod[Any] | None = None

        if inspect.ismethod(callback) and callback.__self__ is not None:
            self._weak = weakref.WeakMethod(callback)
        else:
            self._strong = callback

    def get(self) -> QueryCallback | None:
        if self._weak is not None:
            return cast(QueryCallback | None, self._weak())

        return self._strong


class RemoteQuerySubscription:
    __slots__ = (
        "__weakref__",
        "_buffer",
        "_callback",
        "_client_ref",
        "_data",
        "_disposed",
        "_event_token",
        "_sequence",
        "_server_instance_id",
        "_spec",
        "_sync_callback",
        "_sync_lock",
        "_synchronized",
        "_synchronizing",
        "_token",
    )

    def __init__(
        self,
        client: RemoteQueryClient,
        token: str,
        event_token: str,
        spec: QuerySubscriptionSpec,
        callback: QueryCallback,
        sync_callback: QuerySyncCallback | None = None,
    ) -> None:
        self._client_ref = weakref.ref(client)
        self._token = token
        self._event_token = event_token
        self._spec = spec
        self._callback = _CallbackReference(callback)
        self._sync_callback = sync_callback
        self._sync_lock = asyncio.Lock()
        self._data: QueryResult = {}
        self._server_instance_id: str | None = None
        self._sequence = 0
        self._synchronized = False
        self._synchronizing = False
        self._buffer: list[QueryChangeSet] = []
        self._disposed = False

    async def dispose(self) -> Response[bool] | None:
        if self._disposed:
            return None

        client = self._client_ref()

        if client is None:
            self._mark_disposed()
            return None

        return await client.dispose_query_subscription(self)

    def dispatch(self, change: QueryChangeSet) -> DispatchResult:
        if self._disposed:
            return DispatchResult.CALLBACK_GONE

        if self._callback.get() is None:
            return DispatchResult.CALLBACK_GONE

        if self._synchronizing:
            self._buffer.append(change)
            return DispatchResult.APPLIED

        return self._apply_change(change)

    async def resync(
        self,
        reason: str = "manual",
        *,
        notify: bool = True,
    ) -> None:
        async with self._sync_lock:
            await self._resync(
                reason,
                notify=notify,
            )

    async def _resync(
        self,
        reason: str,
        *,
        notify: bool,
    ) -> None:
        if self._disposed:
            return

        client = self._client_ref()

        if client is None:
            self._mark_disposed()
            return

        self._synchronizing = True
        self._synchronized = False
        self._buffer.clear()

        try:
            response = await client.query_sync_snapshot(self)

            if response.ok is False:
                raise RuntimeError(
                    "Query synchronization was rejected: "
                    f"{response.error}"
                )

            snapshot = response.data
            events = diff_query_results(
                self._data,
                snapshot.data,
            )

            self._data = copy_query_result(snapshot.data)
            self._server_instance_id = snapshot.server_instance_id
            self._sequence = snapshot.sequence

            if notify and events:
                change = QueryChangeSet(
                    server_instance_id=snapshot.server_instance_id,
                    sequence=snapshot.sequence,
                    events=events,
                )

                try:
                    self._invoke_callback(change)
                except Exception:
                    logger.exception(
                        "Remote query callback failed during sync: "
                        "token=%s",
                        self._token,
                    )

            buffered = sorted(
                self._buffer,
                key=lambda value: value.sequence,
            )
            self._buffer.clear()
            self._synchronizing = False

            for change in buffered:
                if change.sequence <= self._sequence:
                    continue

                result = self._apply_change(
                    change,
                    notify=notify,
                )

                if result is not DispatchResult.APPLIED:
                    raise RuntimeError(
                        "Buffered query changes could not be replayed"
                    )

            self._synchronized = True
            self._notify_sync(True, reason)
            logger.debug(
                "Query subscription synchronized: token=%s reason=%s",
                self._token,
                reason,
            )
        except BaseException:
            self._synchronizing = False
            self._synchronized = False
            raise

    def mark_sync_lost(self, reason: str) -> None:
        if self._disposed or not self._synchronized:
            return

        self._synchronized = False
        self._notify_sync(False, reason)
        logger.warning(
            "Query subscription synchronization lost: token=%s "
            "reason=%s",
            self._token,
            reason,
        )

    def audit_matches(
        self,
        server_instance_id: str,
        sequence: int,
        dirty: bool,
    ) -> bool:
        return (
            self._synchronized
            and not dirty
            and self._server_instance_id == server_instance_id
            and self._sequence == sequence
        )

    def _notify_sync(
        self,
        synchronized: bool,
        reason: str,
    ) -> None:
        callback = self._sync_callback

        if callback is None:
            return

        try:
            callback(synchronized, reason)
        except Exception:
            logger.exception(
                "Query synchronization callback failed: token=%s",
                self._token,
            )

    def _apply_change(
        self,
        change: QueryChangeSet,
        *,
        notify: bool = True,
    ) -> DispatchResult:
        if (
            self._server_instance_id is not None
            and change.server_instance_id != self._server_instance_id
        ):
            return DispatchResult.FAILED

        if change.sequence <= self._sequence:
            return DispatchResult.APPLIED

        if change.sequence != self._sequence + 1:
            return DispatchResult.FAILED

        self._data = apply_query_change(self._data, change)
        self._server_instance_id = change.server_instance_id
        self._sequence = change.sequence
        self._synchronized = True

        if notify and change.events:
            try:
                self._invoke_callback(change)
            except Exception:
                logger.exception(
                    "Remote query callback failed: token=%s",
                    self._token,
                )

        return DispatchResult.APPLIED

    def _invoke_callback(self, change: QueryChangeSet) -> None:
        callback = self._callback.get()

        if callback is None:
            raise RuntimeError(
                "Query callback is no longer available"
            )

        result = callback(change)

        if inspect.isawaitable(result):
            close = getattr(result, "close", None)

            if callable(close):
                close()

            raise TypeError(
                "Remote query callbacks must be synchronous"
            )

    def _replace_remote(
        self,
        token: str,
        event_token: str,
    ) -> None:
        if self._disposed:
            raise RuntimeError(
                "Cannot restore a disposed query subscription"
            )

        self._token = token
        self._event_token = event_token
        self._synchronized = False

    def _mark_disposed(self) -> None:
        self._disposed = True
        self._synchronized = False
        self._buffer.clear()

    @property
    def token(self) -> str:
        return self._token

    @property
    def event_token(self) -> str:
        return self._event_token

    @property
    def spec(self) -> QuerySubscriptionSpec:
        return self._spec

    @property
    def query(self) -> Query:
        return self._spec.query

    @property
    def data(self) -> Mapping[str, Mapping[str, Any]]:
        return MappingProxyType(self._data)

    @property
    def callback_alive(self) -> bool:
        return self._callback.get() is not None

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def synchronized(self) -> bool:
        return self._synchronized

    @property
    def server_instance_id(self) -> str | None:
        return self._server_instance_id

    @property
    def sequence(self) -> int:
        return self._sequence
