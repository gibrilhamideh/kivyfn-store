from __future__ import annotations

from typing import Any

import pytest

from kivyfn.store.events import SignalEvent, SignalRowEvent
from kivyfn.store.operations import OperationSource
from kivyfn.store.schema import Signal, SingleSchema
from kivyfn.store.signals import signal_event_from_payload


def test_signal_is_ephemeral_metadata_with_no_default_field_value() -> None:
    class Notifications(SingleSchema):
        ping = Signal[dict[str, Any]]()

    schema = Notifications()
    assert schema.get_signal("ping") is Notifications.ping
    assert schema.defaults() == {}
    assert schema.fields() == {}
    assert not callable(schema.ping)
    assert schema.signal_names() == {"ping"}


def test_canonical_signal_types_are_public_and_have_explicit_cardinality() -> (
    None
):
    single = SignalEvent(
        schema="One", source=OperationSource.COMMAND, signal="ping"
    )
    row = SignalRowEvent(
        schema="Rows", source=OperationSource.COMMAND, id="r", signal="ping"
    )
    assert single.data is None
    assert row.id == "r"
    assert row.data is None


def test_legacy_signal_decoder_preserves_detached_payloads() -> None:
    payload = {
        "schema": "Rows",
        "signal": "ping",
        "id": "r",
        "data": {"nested": [1]},
    }
    with pytest.deprecated_call(match="deprecated"):
        event = signal_event_from_payload(payload)
    data: Any = payload["data"]
    data["nested"].append(2)
    encoded = event.payload()
    encoded["data"]["nested"].append(3)
    assert event.data == {"nested": [1]}
    assert event.id == "r"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"schema": 1},
        {"schema": "One", "signal": 1},
        {"schema": "Rows", "signal": "ping", "id": ""},
        {"schema": "Rows", "signal": "ping", "id": 1},
    ],
)
def test_legacy_signal_decoder_rejects_malformed_boundary_payload(
    payload: Any,
) -> None:
    with pytest.raises(ValueError):
        signal_event_from_payload(payload)
