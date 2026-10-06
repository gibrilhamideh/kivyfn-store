"""Small relay envelopes around the unchanged Store protocol.

See docs/relay-protocol.md. Numbers used for routing are JS-safe integers.
No authentication policy or application schema belongs in this module.
"""

from __future__ import annotations

import json
from typing import Any

RELAY_VERSION = 1
REGISTER_EVENT = "store_register"
FRAME_EVENT = "store_relay"
ACK_EVENT = "store_relay_ack"
MAX_SEQUENCE = 2**53 - 1

# TODO: Add runtime schema discovery for remote clients.
# Fetch a JSON schema catalogue when connecting and build client states
# from its metadata, including fields, basic validation, and signals.
# Support StateManager.create("SchemaName") alongside schema classes.
# Optionally generate Python stubs and TypeScript types for editor support.


def frame_size(value: object, maximum: int) -> int:
    """Bound normalized JSON bytes; reject binary and non-JSON numbers."""
    try:
        size = len(
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(
            "Relay frames must contain finite JSON data"
        ) from error
    if size > maximum:
        raise ValueError("Relay frame exceeds max_frame_bytes")
    return size


def decode_frame(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Relay frame must be an object")
    if not isinstance(value.get("generation"), str):
        raise ValueError("Relay generation must be a string")
    for name, minimum in (("channel_id", 1), ("sequence", 0)):
        number = value.get(name)
        if type(number) is not int or not minimum <= number <= MAX_SEQUENCE:
            raise ValueError(f"Invalid relay {name}")
    kind = value.get("type")
    if not isinstance(kind, str) or kind not in {"open", "request", "close"}:
        raise ValueError("Invalid inbound relay frame type")
    return value
