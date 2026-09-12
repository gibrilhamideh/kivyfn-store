# src/kivyfn/store/client/consumer.py
"""Shared contract between client handlers and confirmed-data consumers.

A StateManager implements this protocol to receive subscription data. The
contract owns no subscriptions, buffers, requests, or manager references.
Those belong to the handlers that register and call consumers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeAlias

from kivyfn.store.protocol.message.responses import (
    CollectionSubscriptionResponse,
    QuerySubscriptionResponse,
    SingleSubscriptionResponse,
)

if TYPE_CHECKING:
    from kivyfn.store.client.subscription import ClientSubscription
    from kivyfn.store.protocol.message.event import EventMessage


SubscriptionResponse: TypeAlias = (
    SingleSubscriptionResponse
    | CollectionSubscriptionResponse
    | QuerySubscriptionResponse
)


class SubscriptionConsumer(Protocol):
    """
    Synchronous callbacks implemented by StateManager or another owner.

    All callbacks run on the client's event loop. Data application stages
    confirmed values without publishing notifications or reentering the
    client. The client advances revisions after every affected consumer
    succeeds, then calls publish(). Raise if application cannot complete;
    the client must obtain fresh snapshots before trusting the cache again.

    Optimistic values remain separate from confirmed data. Implementing
    these methods satisfies the protocol; inheritance is not required.
    """

    def has_cache(self, subscription: ClientSubscription) -> bool:
        """Whether the complete confirmed view is still retained.

        A matching revision alone is insufficient. Return False if required
        data was discarded, including through a weak State registry.
        """
        ...

    def apply_snapshot(
        self, subscription: ClientSubscription, response: SubscriptionResponse
    ) -> None:
        """Stage a full snapshot or verify reuse of the existing cache.

        None in values or rows means reuse already retained confirmed data.
        An empty tuple is a real snapshot and replaces the previous data.
        Validate completeness before reporting successful application.
        """
        ...

    def apply_event(
        self,
        message: EventMessage,
        subscriptions: tuple[ClientSubscription, ...],
    ) -> None:
        """Stage one commit across all supplied subscriptions together.

        The message is filtered to this consumer's covered subscriptions.
        Query dependency revisions may advance without visible row changes;
        they do not certify a complete cache of those dependency schemas.
        """
        ...

    def publish(self, subscriptions: tuple[ClientSubscription, ...]) -> None:
        """Notify observers after staging and revision advancement succeed."""
        ...

    def on_status(
        self, subscription: ClientSubscription, error: Exception | None
    ) -> None:
        """Observe subscription readiness, disconnection, or recovery.

        Read subscription.ready for confirmed subscription readiness.
        An error describes recovery or failure; None carries no error.
        """
        ...
