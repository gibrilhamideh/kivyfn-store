from __future__ import annotations

import pytest

from kivyfn.store.transport.relay.protocol import (
    MAX_SEQUENCE,
    decode_frame,
    frame_size,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": []},
        {"type": {}},
        {"type": "event"},
        {"generation": None},
        {"channel_id": True},
        {"channel_id": 0},
        {"channel_id": MAX_SEQUENCE + 1},
        {"sequence": 1.5},
        {"sequence": False},
        {"sequence": -1},
    ],
)
def test_invalid_routing_fields_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        decode_frame(
            {
                "generation": "G",
                "channel_id": 1,
                "sequence": 0,
                "type": "open",
                **changes,
            }
        )


@pytest.mark.parametrize("value", [b"binary", float("inf"), float("nan")])
def test_non_json_payloads_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        frame_size({"payload": value}, 1024)


def test_javascript_safe_maximum_and_normalized_json_size() -> None:
    frame = {
        "generation": "G",
        "channel_id": MAX_SEQUENCE,
        "sequence": MAX_SEQUENCE,
        "type": "request",
    }
    assert decode_frame(frame) == frame
    assert frame_size({"v": "é"}, 14) == 14
    with pytest.raises(ValueError):
        frame_size({"v": "é"}, 13)
