# src/kivyfn/store/core/operations.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
)


if TYPE_CHECKING:
    from kivyfn.store.core.instruction import (
        InsertInstruction,
        Instruction,
        RemoveInstruction,
        SetInstruction,
        SetRowInstruction,
    )
    from kivyfn.store.node import Node


_UNSET = object()


class Operation:
    def __init__(self, node: Node, instruction: Instruction) -> None:
        self.node = node
        self.instruction = instruction
        self.address = instruction.address
        self.opcode = instruction.opcode
        self.source = instruction.source

    def execute(self) -> StoreEvent:
        raise NotImplementedError()

    def undo(self) -> StoreEvent:
        raise NotImplementedError()

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
        self.new_value = instruction.value
        self._old_value: Any = _UNSET

    def _repr_details(self) -> str:
        return f"key={self.key!r}, value={self.new_value!r}"

    def execute(self) -> SetEvent:
        self._old_value = self.node.set(self.key, self.new_value)

        return SetEvent(
            schema=self.node.schema.name,
            source=self.source,
            key=self.key,
            old=self._old_value,
            new=self.new_value,
        )

    def undo(self) -> SetEvent:
        if self._old_value is _UNSET:
            raise RuntimeError("Cannot undo a SET operation before execution")

        current = self.node.set(self.key, self._old_value)

        return SetEvent(
            schema=self.node.schema.name,
            source=self.source,
            key=self.key,
            old=current,
            new=self._old_value,
        )


class SetRowOperation(Operation):
    def __init__(self, node: Node, instruction: SetRowInstruction) -> None:
        super().__init__(node, instruction)
        self.id = instruction.id
        self.key = instruction.key
        self.new_value = instruction.value
        self._old_value: Any = _UNSET

    def _repr_details(self) -> str:
        return f"id={self.id!r}, key={self.key!r}, value={self.new_value!r}"

    def execute(self) -> SetRowEvent:
        self._old_value = self.node.setrow(self.id, self.key, self.new_value)

        return SetRowEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            key=self.key,
            old=self._old_value,
            new=self.new_value,
        )

    def undo(self) -> SetRowEvent:
        if self._old_value is _UNSET:
            raise RuntimeError(
                "Cannot undo a SETROW operation before execution"
            )

        current = self.node.setrow(self.id, self.key, self._old_value)

        return SetRowEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            key=self.key,
            old=current,
            new=self._old_value,
        )


class InsertOperation(Operation):
    def __init__(self, node: Node, instruction: InsertInstruction) -> None:
        super().__init__(node, instruction)
        self.id = instruction.id
        self.value = dict(instruction.value)

    def _repr_details(self) -> str:
        return f"id={self.id!r}, value={self.value!r}"

    def execute(self) -> InsertEvent:
        self.node.insert(self.id, self.value)
        row = self.node.snapshot(id=self.id)

        if not isinstance(row, dict):
            raise RuntimeError("Inserted Store row could not be read back")

        return InsertEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            new=row,
        )

    def undo(self) -> RemoveEvent:
        row = self.node.remove(self.id)

        return RemoveEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            old=row,
        )


class RemoveOperation(Operation):
    def __init__(self, node: Node, instruction: RemoveInstruction) -> None:
        super().__init__(node, instruction)
        self.id = instruction.id
        self._old_row: dict[str, Any] | None = None

    def _repr_details(self) -> str:
        return f"id={self.id!r}"

    def execute(self) -> RemoveEvent:
        self._old_row = self.node.remove(self.id)

        return RemoveEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            old=self._old_row,
        )

    def undo(self) -> InsertEvent:
        if self._old_row is None:
            raise RuntimeError(
                "Cannot undo a REMOVE operation before execution"
            )

        self.node.insert(self.id, self._old_row)
        row = self.node.snapshot(id=self.id)

        if not isinstance(row, dict):
            raise RuntimeError("Restored Store row could not be read back")

        return InsertEvent(
            schema=self.node.schema.name,
            source=self.source,
            id=self.id,
            new=row,
        )
