# src/kivyfn/store/core/operations.py
from __future__ import annotations

from copy import deepcopy
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


class Operation:
    def __init__(self, node: Node, instruction: Instruction) -> None:
        self.node = node
        self.instruction = instruction
        self.address = instruction.address
        self.opcode = instruction.opcode
        self.source = instruction.source

    def stage(self, data: dict[str, Any]) -> StoreEvent:
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
