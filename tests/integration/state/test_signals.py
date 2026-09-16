# tests/integration/state/test_signals.py
from __future__ import annotations

from typing import Any, cast

from kivyfn.store.client.subscription import SubscriptionUpdate
from kivyfn.store.protocol.message.event import (
    SignalEventAction,
    SignalRowEventAction,
)
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.signal import Signal
from kivyfn.store.state.manager import StateManager


class ControllerSchema(SingleSchema):
    restart = Signal()


class ScaleSchema(ManySchema):
    tare = Signal()


class FakeClient:
    def subscribe(self, source: object, manager: object) -> None:
        pass


def create_manager() -> StateManager:
    return StateManager(cast(Any, FakeClient()))


def test_single_signal_dispatches_to_state() -> None:
    manager = create_manager()
    controller = manager.create(ControllerSchema)

    received: list[Any] = []

    controller.bind(on_restart=lambda _, data: received.append(data))

    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                ControllerSchema,
                (
                    SignalEventAction(
                        signal="restart", data={"source": "test"}
                    ),
                ),
            ),
        )
    )()

    assert received == [{"source": "test"}]


def test_row_signal_dispatches_row_before_collection() -> None:
    manager = create_manager()
    scales = manager.create(ScaleSchema)

    scales.enter("scale-1")
    scale = scales["scale-1"]

    events: list[tuple[str, str, Any]] = []

    scale.bind(on_tare=lambda _, data: events.append(("row", "scale-1", data)))

    scales.bind(
        on_tare=lambda _, row_id, data: events.append(
            ("collection", row_id, data)
        )
    )

    manager.prepare_subscription_updates(
        (
            SubscriptionUpdate(
                ScaleSchema,
                (
                    SignalRowEventAction(
                        row_id="scale-1",
                        signal="tare",
                        data={"source": "button"},
                    ),
                ),
            ),
        )
    )()

    assert events == [
        ("row", "scale-1", {"source": "button"}),
        ("collection", "scale-1", {"source": "button"}),
    ]


def test_collection_has_listener_but_no_signal_emitter() -> None:
    manager = create_manager()
    scales = manager.create(ScaleSchema)

    scales.enter("scale-1")
    scale = scales["scale-1"]

    assert hasattr(scale, "tare")
    assert hasattr(scale, "on_tare")
    assert hasattr(scales, "on_tare")
    assert not hasattr(scales, "tare")
