"""History inspection and position wire validation at the codec boundary."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from kivyfn.store.core.operations import DataRecord
from kivyfn.store.events.event import SignalEvent, SignalRowEvent, StoreSignal
from kivyfn.store.operations import OperationSource
from kivyfn.store.store.history import (
    HistoryEntry,
    HistoryPage,
    HistoryPosition,
    HistorySignal,
    HistoryStatus,
)


def encode_position(position: HistoryPosition) -> dict[str, Any]:
    return asdict(position)


def decode_position(value: object) -> HistoryPosition:
    data = _object(value)
    return HistoryPosition(
        _string(data, "store_instance_id"),
        _integer(data, "revision"),
        _optional_string(data, "entry_id"),
    )


def encode_history(page: HistoryPage) -> dict[str, Any]:
    # JSON's tuple/list equivalence is intentional; records remain detached.
    return asdict(page)


def decode_history(value: object) -> HistoryPage:
    data = _object(value)
    status = _object(data.get("status"))
    return HistoryPage(
        status=HistoryStatus(
            store_instance_id=_string(status, "store_instance_id"),
            revision=_integer(status, "revision"),
            can_undo=_boolean(status, "can_undo"),
            can_redo=_boolean(status, "can_redo"),
            undo_position=decode_position(status.get("undo_position")),
            redo_position=decode_position(status.get("redo_position")),
            barrier=_optional_string(status, "barrier"),
            retained_bytes=_integer(status, "retained_bytes"),
            total_entries=_integer(status, "total_entries"),
            cursor=_integer(status, "cursor"),
            enabled=_boolean(status, "enabled"),
        ),
        entries=tuple(_entry(item) for item in _sequence(data, "entries")),
    )


def _entry(value: object) -> HistoryEntry:
    data = _object(value)
    records = []
    for value in _sequence(data, "records"):
        record = _object(value)
        records.append(
            DataRecord(
                schema=_string(record, "schema"),
                row_id=_optional_string(record, "row_id"),
                key=_optional_string(record, "key"),
                before=record.get("before"),
                after=record.get("after"),
                before_exists=_boolean(record, "before_exists"),
                after_exists=_boolean(record, "after_exists"),
                source=OperationSource(_string(record, "source")),
            )
        )
    signals = []
    for value in _sequence(data, "signals"):
        signal = _object(value)
        signals.append(
            HistorySignal(
                _signal(signal.get("forward")), _signal(signal.get("inverse"))
            )
        )
    return HistoryEntry(
        id=_string(data, "id"),
        transaction_ids=_strings(data, "transaction_ids"),
        origin_client_id=_optional_string(data, "origin_client_id"),
        label=_optional_string(data, "label"),
        group=_optional_string(data, "group"),
        records=tuple(records),
        signals=tuple(signals),
        byte_size=_integer(data, "byte_size"),
    )


def _signal(value: object) -> StoreSignal:
    data = _object(value)
    values = {
        "schema": _string(data, "schema"),
        "source": OperationSource(_string(data, "source")),
        "signal": _string(data, "signal"),
        "data": data.get("data"),
    }
    if "id" in data:
        return SignalRowEvent(id=_string(data, "id"), **values)
    return SignalEvent(**values)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("History payload must be an object")
    return value


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"History {key} must be a non-empty string")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    return None if data.get(key) is None else _string(data, key)


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"History {key} must be a non-negative integer")
    return value


def _boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ValueError(f"History {key} must be a boolean")
    return value


def _sequence(data: dict[str, Any], key: str) -> list[Any] | tuple[Any, ...]:
    value = data.get(key)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"History {key} must be a sequence")
    return value


def _strings(data: dict[str, Any], key: str) -> tuple[str, ...]:
    values = _sequence(data, key)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"History {key} must contain non-empty strings")
    return tuple(values)
