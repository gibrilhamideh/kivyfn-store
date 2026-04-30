from __future__ import annotations
from typing import cast, TYPE_CHECKING

from .instruction import Instruction
from ..events.event import NodeEvent


if TYPE_CHECKING:
    from ..node import Node


class Operation:
    def __init__(self, instruction: Instruction) -> None:
        self.instruction = instruction
        self.address = instruction.address
        self.opcode = instruction.opcode
        self.source = instruction.source

    def execute(self):
        raise NotImplementedError()

    def undo(self):
        raise NotImplementedError()

    # ----------------------------
    # Debug Representation
    # ----------------------------
    def _repr_details(self) -> str:
        return ""

    def __repr__(self) -> str:
        base = f"address={self.address}, source={self.source.name}"

        details = self._repr_details()

        if details:
            return f"{self.__class__.__name__}({base}, {details})"

        return f"{self.__class__.__name__}({base})"


class SetOperation(Operation):
    def __init__(self, node: Node, instruction: Instruction):
        super().__init__(instruction)
        self.node = node
        self.key = cast(str, instruction.key)
        self.new_value = instruction.value
        self.old_value = None

    def _repr_details(self):
        return f"key={self.key!r}, value={self.new_value!r}"

    def execute(self):
        self.old_value = self.node.set(self.key, self.new_value)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            key=self.key,
            old_value=self.old_value,
            new_value=self.new_value,
        )

    def undo(self):
        current = self.node.set(self.key, self.old_value)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            key=self.key,
            old_value=current,
            new_value=self.old_value,
        )


class SetRowOperation(Operation):
    def __init__(self, node: Node, instruction: Instruction):
        super().__init__(instruction)
        self.node = node
        self.id = cast(str, instruction.id)
        self.key = cast(str, instruction.key)
        self.new_value = instruction.value
        self.old_value = None

    def _repr_details(self):
        return f"id={self.id!r}, key={self.key!r}, value={self.new_value!r}"

    def execute(self):
        self.old_value = self.node.setrow(self.id, self.key, self.new_value)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            id=self.id,
            key=self.key,
            old_value=self.old_value,
            new_value=self.new_value,
        )

    def undo(self):
        current = self.node.setrow(self.id, self.key, self.old_value)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            id=self.id,
            key=self.key,
            old_value=current,
            new_value=self.old_value,
        )


class InsertOperation(Operation):
    def __init__(self, node: Node, instruction: Instruction):
        super().__init__(instruction)
        self.node = node
        self.id = cast(str, instruction.id)
        self.value = cast(dict, instruction.value)

    def _repr_details(self):
        return f"id={self.id!r}, value={self.value!r}"

    def execute(self):
        self.node.insert(self.id, self.value)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            id=self.id,
            new_value=self.value,
        )

    def undo(self):
        row = self.node.remove(self.id)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            id=self.id,
            old_value=row,
        )


class RemoveOperation(Operation):
    def __init__(self, node: Node, instruction: Instruction):
        super().__init__(instruction)
        self.node = node
        self.id = cast(str, instruction.id)
        self.old_row = {}

    def _repr_details(self):
        return f"id={self.id!r}"

    def execute(self):
        self.old_row = self.node.remove(self.id)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            id=self.id,
            old_value=self.old_row,
        )

    def undo(self):
        self.node.insert(self.id, self.old_row)

        return NodeEvent(
            opcode=self.opcode,
            address=self.node.address(),
            id=self.id,
            new_value=self.old_row,
        )
