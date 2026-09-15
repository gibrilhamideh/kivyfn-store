# src/kivyfn/store/signals/event.py
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from warnings import warn


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalEvent:
    """Deprecated legacy payload, retained for import compatibility.

    Active Store commits use events.event.SignalEvent and SignalRowEvent.
    """

    schema: str
    signal: str
    id: str | None = None
    data: Any = None

    def __post_init__(self) -> None:
        warn(
            "kivyfn.store.signals.SignalEvent is deprecated; use "
            "kivyfn.store.events SignalEvent or SignalRowEvent",
            DeprecationWarning,
            stacklevel=2,
        )
        if not self.schema:
            raise ValueError("Signal event schema cannot be empty")

        if not self.signal:
            raise ValueError("Signal event name cannot be empty")

        if self.id == "":
            raise ValueError("Signal event row ID cannot be empty")

    def payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "signal": self.signal,
            "id": self.id,
            "data": deepcopy(self.data),
        }


def signal_event_from_payload(payload: dict[str, Any]) -> SignalEvent:
    if not isinstance(payload, dict):
        raise ValueError("Signal event payload must be a dictionary")

    schema = payload.get("schema")
    signal = payload.get("signal")
    row_id = payload.get("id")

    if not isinstance(schema, str) or not schema:
        raise ValueError("Signal event schema must be a non-empty string")

    if not isinstance(signal, str) or not signal:
        raise ValueError("Signal event name must be a non-empty string")

    if row_id is not None and (not isinstance(row_id, str) or not row_id):
        raise ValueError(
            "Signal event row ID must be a non-empty string or None"
        )

    return SignalEvent(
        schema=schema,
        signal=signal,
        id=row_id,
        data=deepcopy(payload.get("data")),
    )
