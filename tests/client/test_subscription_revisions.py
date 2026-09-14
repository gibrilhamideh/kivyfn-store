# tests/client/test_subscription_revisions.py
from __future__ import annotations

from kivyfn.store.client.subscription import ClientSubscription
from kivyfn.store.protocol.message.event import (
    SignalRowEventAction,
    SubscriptionEvent,
)
from kivyfn.store.schema import ManySchema


class DemoSchema(ManySchema):
    pass


class DemoManager:
    def bind_subscription(self, source: object, subscription_id: str) -> None:
        pass

    def unbind_subscription(self, subscription_id: str) -> None:
        pass


def test_signal_only_event_keeps_current_revision() -> None:
    subscription = ClientSubscription(
        source=DemoSchema,
        manager=DemoManager(),
        subscription_id="demo",
        revisions={"DemoSchema": 4},
    )

    event = SubscriptionEvent(
        subscription_id="demo",
        revisions={},
        actions=(
            SignalRowEventAction(row_id="row-1", signal="changed", data=None),
        ),
    )

    assert subscription.next_revisions(event) == {"DemoSchema": 4}


def test_event_may_advance_only_one_query_dependency() -> None:
    subscription = ClientSubscription(
        source=DemoSchema,
        manager=DemoManager(),
        subscription_id="query",
        revisions={"UserSchema": 4, "UserSettingsSchema": 7},
    )

    event = SubscriptionEvent(
        subscription_id="query", revisions={"UserSchema": 5}, actions=()
    )

    assert subscription.next_revisions(event) == {
        "UserSchema": 5,
        "UserSettingsSchema": 7,
    }
