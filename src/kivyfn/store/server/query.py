from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any, Iterable

from kivyfn.logging import get_logger
from kivyfn.store.query import Query, QueryChangeSet
from kivyfn.store.query.event import (
    QueryResult,
    copy_query_result,
    diff_query_results,
)
from kivyfn.store.query.sync import QuerySyncSnapshot
from kivyfn.store.server.subscription import DispatchMessage
from kivyfn.store.utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.query.plan import QueryPlan
    from kivyfn.store.server.revision import StoreRevisionTracker


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QuerySubscribeResult:
    subscription_token: str
    event_token: str
    channel: str
    should_join_channel: bool


@dataclass(frozen=True, slots=True)
class QueryUnsubscribeResult:
    channel: str
    should_leave_channel: bool


@dataclass(slots=True)
class _ServerQuerySubscription:
    token: str
    client_id: str
    channel: str
    plan: QueryPlan
    data: QueryResult
    sequence: int = 0
    dirty: bool = False


class RemoteQuerySubscriptionManager:
    def __init__(
        self,
        engine: Engine,
        revisions: StoreRevisionTracker,
    ) -> None:
        self._engine = engine
        self._revisions = revisions
        self._lock = RLock()
        self._subscriptions: dict[
            str,
            _ServerQuerySubscription,
        ] = {}
        self._client_subscriptions: dict[str, set[str]] = {}

    def fetch(self, query: Query) -> QueryResult:
        return self._engine.fetch(query)

    def subscribe(
        self,
        client_id: str,
        query: Query,
    ) -> QuerySubscribeResult:
        _validate_client_id(client_id)

        with self._lock:
            plan = self._engine.compile_query(query)
            data = self._engine.execute_query(plan)
            token = generate_uuid()
            channel = f"query:{generate_uuid()}"
            subscription = _ServerQuerySubscription(
                token=token,
                client_id=client_id,
                channel=channel,
                plan=plan,
                data=copy_query_result(data),
            )
            self._subscriptions[token] = subscription
            self._client_subscriptions.setdefault(
                client_id,
                set(),
            ).add(token)

        return QuerySubscribeResult(
            subscription_token=token,
            event_token=channel,
            channel=channel,
            should_join_channel=True,
        )

    def unsubscribe(
        self,
        client_id: str,
        token: str,
    ) -> QueryUnsubscribeResult:
        _validate_client_id(client_id)

        with self._lock:
            subscription = self._owned_subscription(
                client_id,
                token,
            )
            self._remove_subscription(subscription)

        return QueryUnsubscribeResult(
            channel=subscription.channel,
            should_leave_channel=True,
        )

    def sync_snapshot(
        self,
        client_id: str,
        token: str,
    ) -> QuerySyncSnapshot:
        _validate_client_id(client_id)

        with self._lock:
            subscription = self._owned_subscription(
                client_id,
                token,
            )
            current = self._engine.execute_query(
                subscription.plan,
            )
            subscription.data = copy_query_result(current)
            subscription.dirty = False

            return QuerySyncSnapshot(
                server_instance_id=self._revisions.instance_id,
                sequence=subscription.sequence,
                data=copy_query_result(current),
            )

    def audit(
        self,
        client_id: str,
        tokens: list[str],
    ) -> dict[str, Any]:
        _validate_client_id(client_id)

        with self._lock:
            values: list[dict[str, Any]] = []

            for token in dict.fromkeys(tokens):
                subscription = self._subscriptions.get(token)

                if (
                    subscription is None
                    or subscription.client_id != client_id
                ):
                    continue

                values.append(
                    {
                        "subscription_token": token,
                        "sequence": subscription.sequence,
                        "dirty": subscription.dirty,
                    }
                )

        return {
            "server_instance_id": self._revisions.instance_id,
            "subscriptions": values,
        }

    def remove_client(self, client_id: str) -> None:
        _validate_client_id(client_id)

        with self._lock:
            tokens = tuple(
                self._client_subscriptions.pop(client_id, ())
            )

            for token in tokens:
                self._subscriptions.pop(token, None)

    def dispatch(
        self,
        events: Iterable[StoreEvent],
    ) -> tuple[DispatchMessage, ...]:
        event_values = tuple(events)
        addresses = {event.address for event in event_values}

        if not addresses:
            return ()

        messages: list[DispatchMessage] = []

        with self._lock:
            subscriptions = tuple(self._subscriptions.values())

            for subscription in subscriptions:
                if addresses.isdisjoint(
                    subscription.plan.dependencies
                ):
                    continue

                try:
                    current = self._engine.execute_query(
                        subscription.plan,
                    )
                    query_events = diff_query_results(
                        subscription.data,
                        current,
                    )
                except Exception:
                    subscription.dirty = True
                    logger.exception(
                        "Could not refresh query subscription %s",
                        subscription.token,
                    )
                    continue

                subscription.data = copy_query_result(current)
                subscription.dirty = False

                if not query_events:
                    continue

                subscription.sequence += 1
                change = QueryChangeSet(
                    server_instance_id=self._revisions.instance_id,
                    sequence=subscription.sequence,
                    events=query_events,
                )
                messages.append(
                    DispatchMessage(
                        channel=subscription.channel,
                        payload={
                            "event_token": subscription.channel,
                            "query_change": change.payload(),
                        },
                    )
                )

        return tuple(messages)

    def _owned_subscription(
        self,
        client_id: str,
        token: str,
    ) -> _ServerQuerySubscription:
        if not isinstance(token, str) or not token:
            raise ValueError(
                "Query subscription token must be non-empty"
            )

        subscription = self._subscriptions.get(token)

        if subscription is None or subscription.client_id != client_id:
            raise KeyError(
                "Query subscription not found for this client"
            )

        return subscription

    def _remove_subscription(
        self,
        subscription: _ServerQuerySubscription,
    ) -> None:
        self._subscriptions.pop(subscription.token, None)
        tokens = self._client_subscriptions.get(
            subscription.client_id
        )

        if tokens is None:
            return

        tokens.discard(subscription.token)

        if not tokens:
            self._client_subscriptions.pop(
                subscription.client_id,
                None,
            )


def _validate_client_id(client_id: str) -> None:
    if not isinstance(client_id, str) or not client_id:
        raise ValueError(
            "Query subscription client ID must be non-empty"
        )
