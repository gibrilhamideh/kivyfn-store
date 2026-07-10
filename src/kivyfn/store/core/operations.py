from __future__ import annotations
from typing import TYPE_CHECKING, Any, cast

from kivyfn.store.events.event import (
    InsertEvent,
    RemoveEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
)


if TYPE_CHECKING:
    from ..node import Node
    from kivyfn.store.core.instruction import Instruction


class Operation:
    def __init__(
        self,
        node: Node,
        instruction: Instruction,
    ) -> None:
        self.node = node
        self.instruction = instruction
        self.address = instruction.address
        self.opcode = instruction.opcode
        self.source = instruction.source

    def execute(self) -> StoreEvent:
        raise NotImplementedError()

    def undo(self) -> StoreEvent:
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
    def __init__(
        self,
        node: Node,
        instruction: Instruction,
    ) -> None:
        super().__init__(node, instruction)
        self.key = cast(str, instruction.key)
        self.new_value = instruction.value
        self.old_value: Any = None

    def _repr_details(self) -> str:
        return f"key={self.key!r}, value={self.new_value!r}"

    def execute(self) -> SetEvent:
        self.old_value = self.node.set(
            self.key,
            self.new_value,
        )

        return SetEvent(
            address=self.node.address,
            key=self.key,
            old_value=self.old_value,
            new_value=self.new_value,
        )

    def undo(self) -> SetEvent:
        current = self.node.set(
            self.key,
            self.old_value,
        )

        return SetEvent(
            address=self.node.address,
            key=self.key,
            old_value=current,
            new_value=self.old_value,
        )


class SetRowOperation(Operation):
    def __init__(
        self,
        node: Node,
        instruction: Instruction,
    ) -> None:
        super().__init__(node, instruction)
        self.id = cast(str, instruction.id)
        self.key = cast(str, instruction.key)
        self.new_value = instruction.value
        self.old_value: Any = None

    def _repr_details(self) -> str:
        return (
            f"id={self.id!r}, "
            f"key={self.key!r}, "
            f"value={self.new_value!r}"
        )

    def execute(self) -> SetRowEvent:
        self.old_value = self.node.setrow(
            self.id,
            self.key,
            self.new_value,
        )

        return SetRowEvent(
            address=self.node.address,
            id=self.id,
            key=self.key,
            old_value=self.old_value,
            new_value=self.new_value,
        )

    def undo(self) -> SetRowEvent:
        current = self.node.setrow(
            self.id,
            self.key,
            self.old_value,
        )

        return SetRowEvent(
            address=self.node.address,
            id=self.id,
            key=self.key,
            old_value=current,
            new_value=self.old_value,
        )


class InsertOperation(Operation):
    def __init__(
        self,
        node: Node,
        instruction: Instruction,
    ) -> None:
        super().__init__(node, instruction)
        self.id = cast(str, instruction.id)
        self.value = cast(dict[str, Any], instruction.value)

    def _repr_details(self) -> str:
        return f"id={self.id!r}, value={self.value!r}"

    def execute(self) -> InsertEvent[dict[str, Any]]:
        self.node.insert(
            self.id,
            self.value,
        )

        return InsertEvent(
            address=self.node.address,
            id=self.id,
            new_value=self.value,
        )

    def undo(self) -> RemoveEvent[dict[str, Any]]:
        row = self.node.remove(self.id)

        return RemoveEvent(
            address=self.node.address,
            id=self.id,
            old_value=row,
        )


class RemoveOperation(Operation):
    def __init__(
        self,
        node: Node,
        instruction: Instruction,
    ) -> None:
        super().__init__(node, instruction)
        self.id = cast(str, instruction.id)
        self.old_row: dict[str, Any] = {}

    def _repr_details(self) -> str:
        return f"id={self.id!r}"

    def execute(self) -> RemoveEvent[dict[str, Any]]:
        self.old_row = self.node.remove(self.id)

        return RemoveEvent(
            address=self.node.address,
            id=self.id,
            old_value=self.old_row,
        )

    def undo(self) -> InsertEvent[dict[str, Any]]:
        self.node.insert(
            self.id,
            self.old_row,
        )

        return InsertEvent(
            address=self.node.address,
            id=self.id,
            new_value=self.old_row,
        )
