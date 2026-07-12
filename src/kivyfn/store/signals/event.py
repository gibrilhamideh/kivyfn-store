from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from kivyfn.store.address import Address


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SignalEvent(Generic[T]):
    address: Address[Any]
    signal: str
    id: str | None
    data: T

    def payload(self) -> dict[str, Any]:
        return {
            "address": self.address.to_list(),
            "signal": self.signal,
            "id": self.id,
            "data": self.data,
        }


def signal_event_from_payload(
    payload: dict[str, Any],
) -> SignalEvent[Any]:
    return SignalEvent(
        address=Address(*payload["address"]),
        signal=payload["signal"],
        id=payload.get("id"),
        data=payload.get("data"),
    )
