# src/kivyfn/store/core/operations.py
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kivyfn.store.core.instruction import (
    InsertInstruction,
    Instruction,
    RemoveInstruction,
    SetInstruction,
    SetRowInstruction,
)
from kivyfn.store.error import HistoryError, HistoryErrorDetail
from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
)
from kivyfn.store.operations import OperationSource
from kivyfn.store.schema import SingleSchema

if TYPE_CHECKING:
    from kivyfn.store.core.delta import Delta
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


class Operation:
    def __init__(self, node: Node, instruction: Instruction) -> None:
        self.node = node
        self.instruction = instruction
        self.address = instruction.address
        self.opcode = instruction.opcode
        self.source = instruction.source

    def stage(self, data: dict[str, Any]) -> StoreEvent:
        raise NotImplementedError()

    @staticmethod
    def undo(record: DataRecord, engine: Engine, delta: Delta) -> None:
        Operation.restore(record, engine, delta, undo=True)

    @staticmethod
    def redo(record: DataRecord, engine: Engine, delta: Delta) -> None:
        Operation.restore(record, engine, delta, undo=False)

    @staticmethod
    def restore(
        record: DataRecord, engine: Engine, delta: Delta, *, undo: bool
    ) -> None:
        node = engine.get_node_by_schema(record.schema)
        value = record.before if undo else record.after
        exists = record.before_exists if undo else record.after_exists
        operation: Operation
        if record.row_id is None:
            assert record.key is not None
            operation = SetOperation(
                node,
                SetInstruction(
                    address=node.address,
                    key=record.key,
                    value=value,
                    source=record.source,
                ),
            )
        elif record.key is not None:
            operation = SetRowOperation(
                node,
                SetRowInstruction(
                    address=node.address,
                    id=record.row_id,
                    key=record.key,
                    value=value,
                    source=record.source,
                ),
            )
        elif exists:
            operation = InsertOperation(
                node,
                InsertInstruction(
                    address=node.address,
                    id=record.row_id,
                    value=value,
                    source=record.source,
                ),
            )
        else:
            operation = RemoveOperation(
                node,
                RemoveInstruction(
                    address=node.address,
                    id=record.row_id,
                    source=record.source,
                ),
            )
        delta.add(operation)

    def _repr_details(self) -> str:
        return ""

    def __repr__(self) -> str:
        base = f"address={self.address}, source={self.source.name}"
        details = self._repr_details()

        if details:
            return f"{type(self).__name__}({base}, {details})"

        return f"{type(self).__name__}({base})"


class SetOperation(Operation):
    def __init__(self, node: Node, instruction: SetInstruction) -> None:
        super().__init__(node, instruction)
        self.key = instruction.key
        self.new_value = deepcopy(instruction.value)

    def _repr_details(self) -> str:
        return f"key={self.key!r}, value={self.new_value!r}"

    def stage(self, data: dict[str, Any]) -> SetEvent:
        old_value = data.get(self.key)
        data[self.key] = deepcopy(self.new_value)

        return SetEvent(
            schema=self.node.schema.name,
            source=self.source,
            key=self.key,
            old=deepcopy(old_value),
            new=deepcopy(self.new_value),
        )


class SetRowOperation(Operation):
    def __init__(self, node: Node, instruction: SetRowInstruction) -> None:
        super().__init__(node, instruction)
        self.id = instruction.id
        self.key = instruction.key
        self.new_value = deepcopy(instruction.value)

    def _repr_details(self) -> str:
        return f"id={self.id!r}, key={self.key!r}, value={self.new_value!r}"

    def stage(self, data: dict[str, Any]) -> SetRowEvent:
        old_value = data[self.id].get(self.key)
        data[self.id][self.key] = deepcopy(self.new_value)

        return SetRowEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            key=self.key,
            old=deepcopy(old_value),
            new=deepcopy(self.new_value),
        )


class InsertOperation(Operation):
    def __init__(self, node: Node, instruction: InsertInstruction) -> None:
        super().__init__(node, instruction)
        self.id = instruction.id
        self.value = deepcopy(instruction.value)

    def _repr_details(self) -> str:
        return f"id={self.id!r}, value={self.value!r}"

    def stage(self, data: dict[str, Any]) -> InsertEvent:
        data[self.id] = deepcopy(self.value)

        return InsertEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            new=deepcopy(self.value),
        )


class RemoveOperation(Operation):
    def __init__(self, node: Node, instruction: RemoveInstruction) -> None:
        super().__init__(node, instruction)
        self.id = instruction.id

    def _repr_details(self) -> str:
        return f"id={self.id!r}"

    def stage(self, data: dict[str, Any]) -> RemoveEvent:
        old_row = data.pop(self.id)

        return RemoveEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            old=deepcopy(old_row),
        )


@dataclass(frozen=True, slots=True)
class DataRecord:
    """One final transformation, detached from its original Operation."""

    schema: str
    row_id: str | None
    key: str | None
    before: Any
    after: Any
    before_exists: bool = True
    after_exists: bool = True
    source: OperationSource = OperationSource.COMMAND

    def guard(self, engine: Engine, *, undo: bool) -> None:
        data = engine.get_node_by_schema(self.schema).value
        expected = self.after if undo else self.before
        exists = self.after_exists if undo else self.before_exists
        if self.row_id is not None:
            if (self.row_id in data) != exists:
                self._conflict(expected, data.get(self.row_id))
            if not exists:
                return
            data = data[self.row_id]
        if self.key is None:
            actual = data
        elif self.key in data:
            actual = data[self.key]
        else:
            self._conflict(expected, None)
            return
        if actual != expected:
            self._conflict(expected, actual)

    def _conflict(self, expected: Any, actual: Any) -> None:
        raise HistoryError(
            "Recorded value no longer matches current Store data",
            detail=HistoryErrorDetail(
                schema=self.schema,
                row_id=self.row_id,
                field=self.key,
                expected=deepcopy(expected),
                value=deepcopy(actual),
            ),
        )


def normalize_records(
    delta: Delta, copy: Callable[[Any], Any] = deepcopy
) -> tuple[DataRecord, ...]:
    """Select first-before/final-after; callers bound copies incrementally."""
    records: list[DataRecord] = []
    changed: dict[
        str, dict[str | None, dict[str | None, OperationSource]]
    ] = {}
    for event in delta.events:
        row_id = None if isinstance(event, SetEvent) else event.id
        key = event.key if isinstance(event, (SetEvent, SetRowEvent)) else None
        rows = changed.setdefault(event.schema, {})
        rows.setdefault(row_id, {})[key] = event.source

    for node, after in delta.values.items():
        before = node.value
        schema = node.schema.name
        if isinstance(node.schema, SingleSchema):
            for key, source in changed[schema][None].items():
                assert key is not None
                old = before[key]
                if old != after[key]:
                    records.append(
                        copy(
                            DataRecord(
                                schema,
                                None,
                                key,
                                old,
                                after[key],
                                source=source,
                            )
                        )
                    )
            continue
        for row_id, fields in changed[schema].items():
            assert row_id is not None
            before_exists = row_id in before
            after_exists = row_id in after
            if before_exists and after_exists:
                # Replacing a row can change every field. Ordinary writes
                # inspect only that row's actual field effects, avoiding
                # unrelated cyclic or opaque values in staged row copies.
                keys = before[row_id] if None in fields else fields
                structural_source = fields.get(None, OperationSource.COMMAND)
                for key in keys:
                    assert key is not None
                    old = before[row_id][key]
                    if old != after[row_id][key]:
                        records.append(
                            copy(
                                DataRecord(
                                    schema,
                                    row_id,
                                    key,
                                    old,
                                    after[row_id][key],
                                    source=fields.get(key, structural_source),
                                )
                            )
                        )
            elif before_exists != after_exists:
                records.append(
                    copy(
                        DataRecord(
                            schema,
                            row_id,
                            None,
                            before.get(row_id),
                            after.get(row_id),
                            before_exists,
                            after_exists,
                            fields[None],
                        )
                    )
                )
    return tuple(records)
