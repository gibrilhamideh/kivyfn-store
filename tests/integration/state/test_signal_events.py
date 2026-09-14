# tests/codec/test_signal_events.py
from __future__ import annotations

from kivyfn.store.codec.events import (
    decode_event_message,
    encode_event_message,
)
from kivyfn.store.protocol.message.event import (
    EventMessage,
    SignalEventAction,
    SignalRowEventAction,
    SubscriptionEvent,
)


def test_single_signal_event_round_trip_without_revision() -> None:
    message = EventMessage(
        store_instance_id="store-1",
        events=(
            SubscriptionEvent(
                subscription_id="controller",
                revisions={},
                actions=(
                    SignalEventAction(
                        signal="restart", data={"source": "test"}
                    ),
                ),
            ),
        ),
    )

    assert decode_event_message(encode_event_message(message)) == message


def test_row_signal_event_round_trip_without_revision() -> None:
    message = EventMessage(
        store_instance_id="store-1",
        events=(
            SubscriptionEvent(
                subscription_id="scales",
                revisions={},
                actions=(
                    SignalRowEventAction(
                        row_id="scale-1",
                        signal="tare",
                        data={"source": "button"},
                    ),
                ),
            ),
        ),
    )

    assert decode_event_message(encode_event_message(message)) == message
