# src/kivyfn/store/protocol/message/event.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True)
class SetEventAction:
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SetRowEventAction:
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class InsertEventAction:
    row_id: str
    values: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RemoveEventAction:
    row_id: str


@dataclass(frozen=True, slots=True)
class SignalEventAction:
    signal: str
    data: Any = None
    occurrence_id: int | None = None


@dataclass(frozen=True, slots=True)
class SignalRowEventAction:
    row_id: str
    signal: str
    data: Any = None
    occurrence_id: int | None = None


EventAction: TypeAlias = (
    SetEventAction
    | SetRowEventAction
    | InsertEventAction
    | RemoveEventAction
    | SignalEventAction
    | SignalRowEventAction
)


@dataclass(frozen=True, slots=True)
class SubscriptionEvent:
    """One canonical update for one active subscription."""

    subscription_id: str
    revisions: dict[str, int]
    actions: tuple[EventAction, ...]


@dataclass(frozen=True, slots=True)
class EventMessage:
    """One ordered group of subscription updates from one Store."""

    store_instance_id: str
    events: tuple[SubscriptionEvent, ...]
    transaction_id: str | None = None
