"""Bounded, instance-local history. Records never retain Nodes or requests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass, replace
from math import isnan
from sys import getsizeof
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from kivyfn.store.core.operations import DataRecord, normalize_records
from kivyfn.store.events.event import SignalRowEvent, StoreSignal

if TYPE_CHECKING:
    from kivyfn.store.builder.config import HistoryConfig
    from kivyfn.store.core.delta import Delta
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.core.execution import ExecutionResult


@dataclass(frozen=True, slots=True)
class HistoryPosition:
    store_instance_id: str
    revision: int
    entry_id: str | None


@dataclass(frozen=True, slots=True)
class HistorySignal:
    forward: StoreSignal
    inverse: StoreSignal


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: str
    transaction_ids: tuple[str, ...]
    origin_client_id: str | None
    label: str | None
    group: str | None
    records: tuple[DataRecord, ...]
    signals: tuple[HistorySignal, ...]
    byte_size: int = 0


@dataclass(frozen=True, slots=True)
class HistoryStatus:
    store_instance_id: str
    revision: int
    can_undo: bool
    can_redo: bool
    undo_position: HistoryPosition
    redo_position: HistoryPosition
    barrier: str | None
    retained_bytes: int
    total_entries: int
    cursor: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class HistoryPage:
    status: HistoryStatus
    entries: tuple[HistoryEntry, ...]


Footprint = tuple[str, str | None, str | None]


@dataclass(frozen=True, slots=True)
class HistoryState:
    entries: tuple[HistoryEntry, ...] = ()
    cursor: int = 0
    revision: int = 0
    barrier: str | None = None
    open_entry: str | None = None
    intervening: frozenset[Footprint] = frozenset()


class History:
    """Prepare retention before persistence; install with canonical data."""

    def __init__(self, instance_id: str, config: HistoryConfig | None) -> None:
        self.instance_id = instance_id
        self.config = config
        self.state = HistoryState()

    def status(self) -> HistoryStatus:
        state = self.state
        undo = state.entries[state.cursor - 1] if state.cursor else None
        redo = (
            state.entries[state.cursor]
            if state.cursor < len(state.entries)
            else None
        )
        return HistoryStatus(
            self.instance_id,
            state.revision,
            undo is not None,
            redo is not None,
            HistoryPosition(
                self.instance_id, state.revision, undo.id if undo else None
            ),
            HistoryPosition(
                self.instance_id, state.revision, redo.id if redo else None
            ),
            state.barrier,
            self.retained_size(state),
            len(state.entries),
            state.cursor,
            self.config is not None,
        )

    def page(self, limit: int, offset: int) -> HistoryPage:
        return HistoryPage(
            self.status(),
            deepcopy(self.state.entries[offset : offset + limit]),
        )

    @staticmethod
    def retained_size(state: HistoryState) -> int:
        return sum(entry.byte_size for entry in state.entries) + (
            retained_size(state.intervening) if state.intervening else 0
        )

    def close_group(self, origin_client_id: str | None = None) -> None:
        state = self.state
        if not state.open_entry:
            return
        if origin_client_id is not None and (
            state.entries[-1].origin_client_id != origin_client_id
        ):
            return
        self.state = self._closed(state)

    @staticmethod
    def _closed(state: HistoryState) -> HistoryState:
        return replace(
            state,
            revision=state.revision + 1,
            open_entry=None,
            intervening=frozenset(),
        )

    def prepare(
        self,
        engine: Engine,
        delta: Delta,
        result: ExecutionResult,
        signals: tuple[StoreSignal, ...],
        *,
        transaction_id: str,
        origin_client_id: str | None,
        group: str | None,
        label: str | None,
    ) -> tuple[HistoryState, str | None]:
        config = self.config
        state = self.state
        if config is None:
            return state, None
        selected = any(
            operation.node.schema.track_history
            for operation in result.compiled
        ) or any(
            engine.get_node_by_schema(signal.schema).schema.track_history
            for signal in signals
        )
        footprint = (
            execution_footprint(result, config.max_bytes)
            if state.open_entry
            else frozenset()
        )
        if footprint is None:
            state = replace(state, open_entry=None, intervening=frozenset())
        if not selected:
            return self._intervene(footprint), None
        try:
            budget = Budget(config.max_bytes)
            records = normalize_records(delta, budget.copy)
            captured: list[HistorySignal] = []
            for signal in signals:
                node = engine.get_node_by_schema(signal.schema)
                inverse_name = node.schema.get_signal(signal.signal).undo
                if inverse_name is None:
                    return self._barrier(
                        "Signal has no declared inverse"
                    ), None
                if isinstance(signal, SignalRowEvent) and (
                    signal.id not in node.value
                    or signal.id not in delta.data(node)
                ):
                    return self._barrier(
                        "Signal target lacks both lifetimes"
                    ), None
                captured.append(
                    budget.copy(
                        HistorySignal(
                            signal, replace(signal, signal=inverse_name)
                        )
                    )
                )
            if not records and not captured:
                return self._intervene(footprint), None
            entry = HistoryEntry(
                uuid4().hex,
                (transaction_id,),
                origin_client_id,
                label,
                group if origin_client_id is not None else None,
                records,
                tuple(captured),
            )
            entry = sized_entry(entry, config.max_bytes)
        except CapacityExceeded:
            return self._barrier(
                "Transaction exceeds history byte limit"
            ), None
        except (UnsupportedHistoryValue, RecursionError):
            return self._barrier(
                "Cyclic, NaN, or opaque value cannot be restored safely"
            ), None

        entries = state.entries[: state.cursor]
        can_merge = bool(
            entries
            and state.open_entry == entries[-1].id
            and entry.group
            and entries[-1].group == entry.group
            and entries[-1].origin_client_id == origin_client_id
            and footprint is not None
            and not overlaps(footprint, state.intervening)
        )
        merged_segment = False
        if can_merge:
            merged = self._merge(engine, entries[-1], entry)
            if merged is not None:
                entries = entries[:-1]
                entry = merged
                merged_segment = True
        entries += (entry,)
        if not entry.records and not entry.signals:
            entries = entries[:-1]
            entry_id = None
        else:
            entry_id = entry.id
        intervening = (
            state.intervening if merged_segment and entry_id else frozenset()
        )
        footprint_bytes = retained_size(intervening) if intervening else 0
        while len(entries) > config.max_entries or (
            sum(item.byte_size for item in entries) + footprint_bytes
            > config.max_bytes
        ):
            entries = entries[1:]
        return HistoryState(
            entries=entries,
            cursor=len(entries),
            revision=state.revision + 1,
            barrier=state.barrier,
            open_entry=entry_id if entry.group else None,
            intervening=intervening,
        ), entry_id

    def _barrier(self, reason: str) -> HistoryState:
        return HistoryState(revision=self.state.revision + 1, barrier=reason)

    def _intervene(
        self, footprint: frozenset[Footprint] | None
    ) -> HistoryState:
        state = self.state
        if not state.open_entry or footprint == frozenset():
            return state
        if footprint is None:
            return self._closed(state)
        if overlaps(footprint, entry_footprint(state.entries[-1])):
            return self._closed(state)
        proposed = replace(state, intervening=state.intervening | footprint)
        assert self.config is not None
        if self.retained_size(proposed) > self.config.max_bytes:
            return self._closed(state)
        return proposed

    def _merge(
        self, engine: Engine, first: HistoryEntry, last: HistoryEntry
    ) -> HistoryEntry | None:
        assert self.config is not None
        if overlaps(entry_footprint(last), self.state.intervening):
            return None
        records = merge_records(first.records, last.records)
        signals = first.signals + last.signals
        for occurrence in signals:
            signal = occurrence.forward
            if not isinstance(signal, SignalRowEvent):
                continue
            before = after = signal.id in (
                engine.get_node_by_schema(signal.schema).value
            )
            for record in records:
                if (
                    record.schema == signal.schema
                    and record.row_id == signal.id
                    and record.key is None
                ):
                    before = record.before_exists
                    after = record.after_exists
            if not before or not after:
                return None
        entry = replace(
            first,
            transaction_ids=first.transaction_ids + last.transaction_ids,
            records=records,
            signals=signals,
            byte_size=0,
        )
        try:
            overhead = (
                retained_size(self.state.intervening)
                if self.state.intervening
                else 0
            )
            return sized_entry(entry, self.config.max_bytes - overhead)
        except CapacityExceeded:
            return None


class CapacityExceeded(Exception):
    pass


class UnsupportedHistoryValue(Exception):
    pass


class Budget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def copy(self, value: Any) -> Any:
        size = retained_size(value, self.remaining)
        self.remaining -= size
        return deepcopy(value)


def retained_size(
    value: Any,
    limit: int | None = None,
    ancestors: frozenset[int] = frozenset(),
) -> int:
    """Bound traversal before copying; count both payload directions."""
    size = getsizeof(value)
    if limit is not None and size > limit:
        raise CapacityExceeded
    if isinstance(value, float) and isnan(value):
        raise UnsupportedHistoryValue
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return size
    identity = id(value)
    if identity in ancestors:
        raise UnsupportedHistoryValue
    ancestors = ancestors | {identity}
    if is_dataclass(value) and not isinstance(value, type):
        children = (getattr(value, item.name) for item in fields(value))
    elif isinstance(value, dict):
        children = (item for pair in value.items() for item in pair)
    elif isinstance(value, (tuple, list, set, frozenset)):
        children = iter(value)
    elif isinstance(value, bytearray):
        return size
    else:
        raise UnsupportedHistoryValue
    for child in children:
        remaining = None if limit is None else limit - size
        size += retained_size(child, remaining, ancestors)
    return size


def sized_entry(entry: HistoryEntry, limit: int) -> HistoryEntry:
    return replace(entry, byte_size=retained_size(entry, limit))


def execution_footprint(
    result: ExecutionResult, limit: int
) -> frozenset[Footprint] | None:
    locations: set[Footprint] = set()
    remaining = limit
    for operation in result.compiled:
        schema = operation.node.schema.name
        row_id = getattr(operation, "id", None)
        key = getattr(operation, "key", None)
        footprint = (schema, row_id, key)
        if footprint not in locations:
            remaining -= retained_size(footprint)
            if remaining < 0:
                return None
            locations.add(footprint)
    return frozenset(locations)


def entry_footprint(entry: HistoryEntry) -> frozenset[Footprint]:
    return frozenset(
        (record.schema, record.row_id, record.key) for record in entry.records
    )


def overlaps(a: frozenset[Footprint], b: frozenset[Footprint]) -> bool:
    for left in a:
        for right in b:
            if left[:2] == right[:2] and (
                left[2] == right[2] or left[2] is None or right[2] is None
            ):
                return True
    return False


def merge_records(
    first: tuple[DataRecord, ...], last: tuple[DataRecord, ...]
) -> tuple[DataRecord, ...]:
    """Compose row transformations without replaying intermediate writes."""
    groups: dict[tuple[str, str | None], list[DataRecord]] = {}
    for record in first + last:
        groups.setdefault((record.schema, record.row_id), []).append(record)
    merged: list[DataRecord] = []
    for records in groups.values():
        structural = next((r for r in records if r.key is None), None)
        if structural is None:
            values: dict[str | None, DataRecord] = {}
            for record in records:
                previous = values.get(record.key)
                values[record.key] = replace(
                    record,
                    before=previous.before if previous else record.before,
                )
            merged.extend(r for r in values.values() if r.before != r.after)
            continue
        index = records.index(structural)
        before_exists = structural.before_exists
        before = deepcopy(structural.before)
        if before_exists:
            for record in reversed(records[:index]):
                before[record.key] = deepcopy(record.before)
        after_exists = structural.after_exists
        after = deepcopy(structural.after)
        for record in records[index + 1 :]:
            if record.key is None:
                after_exists, after = (
                    record.after_exists,
                    deepcopy(record.after),
                )
            elif after_exists:
                after[record.key] = deepcopy(record.after)
        if before_exists and after_exists:
            for key in before:
                if before[key] != after[key]:
                    merged.append(
                        replace(
                            structural,
                            key=key,
                            before=before[key],
                            after=after[key],
                            before_exists=True,
                            after_exists=True,
                        )
                    )
        elif before_exists != after_exists:
            merged.append(
                replace(
                    structural,
                    before=before,
                    after=after,
                    before_exists=before_exists,
                    after_exists=after_exists,
                )
            )
    return tuple(merged)
