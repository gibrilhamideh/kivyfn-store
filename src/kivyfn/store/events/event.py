# kivyfn/store/events/event.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, TypeAlias, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.constants import InstructionOpCode


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SetEvent:
    """
    Scalar update on a many=False schema.
    """

    address: Address[Any]

    key: str
    old_value: Any
    new_value: Any

    opcode: Literal[InstructionOpCode.SET] = field(
        init=False,
        default=InstructionOpCode.SET,
    )

    def payload(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode.value,
            "address": self.address.to_list(),
            "id": None,
            "key": self.key,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }

    def copy_with(
        self,
        **changes: Any,
    ) -> SetEvent:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class SetRowEvent:
    """
    Scalar update on one row of a many=True schema.
    """

    address: Address[Any]

    id: str
    key: str
    old_value: Any
    new_value: Any

    opcode: Literal[InstructionOpCode.SETROW] = field(
        init=False,
        default=InstructionOpCode.SETROW,
    )

    def payload(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode.value,
            "address": self.address.to_list(),
            "id": self.id,
            "key": self.key,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }

    def copy_with(
        self,
        **changes: Any,
    ) -> SetRowEvent:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class InsertEvent(Generic[T]):
    """
    Insertion of one row into a many=True schema.
    """

    address: Address[T]

    id: str
    new_value: T

    opcode: Literal[InstructionOpCode.INSERT] = field(
        init=False,
        default=InstructionOpCode.INSERT,
    )

    def payload(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode.value,
            "address": self.address.to_list(),
            "id": self.id,
            "key": None,
            "old_value": None,
            "new_value": self.new_value,
        }

    def copy_with(
        self,
        **changes: Any,
    ) -> InsertEvent[T]:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class RemoveEvent(Generic[T]):
    """
    Removal of one row from a many=True schema.
    """

    address: Address[T]

    id: str
    old_value: T

    opcode: Literal[InstructionOpCode.REMOVE] = field(
        init=False,
        default=InstructionOpCode.REMOVE,
    )

    def payload(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode.value,
            "address": self.address.to_list(),
            "id": self.id,
            "key": None,
            "old_value": self.old_value,
            "new_value": None,
        }

    def copy_with(
        self,
        **changes: Any,
    ) -> RemoveEvent[T]:
        return replace(self, **changes)


StoreEvent: TypeAlias = (
    SetEvent
    | SetRowEvent
    | InsertEvent[Any]
    | RemoveEvent[Any]
)

def event_from_payload(
    payload: dict[str, Any],
) -> StoreEvent:
    opcode = InstructionOpCode(payload["opcode"])
    address = Address(*payload["address"])

    if opcode is InstructionOpCode.SET:
        return SetEvent(
            address=address,
            key=payload["key"],
            old_value=payload["old_value"],
            new_value=payload["new_value"],
        )

    if opcode is InstructionOpCode.SETROW:
        return SetRowEvent(
            address=address,
            id=payload["id"],
            key=payload["key"],
            old_value=payload["old_value"],
            new_value=payload["new_value"],
        )

    if opcode is InstructionOpCode.INSERT:
        return InsertEvent(
            address=address,
            id=payload["id"],
            new_value=payload["new_value"],
        )

    return RemoveEvent(
        address=address,
        id=payload["id"],
        old_value=payload["old_value"],
    )
