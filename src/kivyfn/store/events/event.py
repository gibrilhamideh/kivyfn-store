# kivyfn/store/events/event.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, TypeAlias, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.operations import OpCode


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

    opcode: Literal[OpCode.SET] = field(
        init=False,
        default=OpCode.SET,
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

    opcode: Literal[OpCode.SETROW] = field(
        init=False,
        default=OpCode.SETROW,
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

    opcode: Literal[OpCode.INSERT] = field(
        init=False,
        default=OpCode.INSERT,
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

    opcode: Literal[OpCode.REMOVE] = field(
        init=False,
        default=OpCode.REMOVE,
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
    opcode = OpCode(payload["opcode"])
    address = Address(*payload["address"])

    if opcode is OpCode.SET:
        return SetEvent(
            address=address,
            key=payload["key"],
            old_value=payload["old_value"],
            new_value=payload["new_value"],
        )

    if opcode is OpCode.SETROW:
        return SetRowEvent(
            address=address,
            id=payload["id"],
            key=payload["key"],
            old_value=payload["old_value"],
            new_value=payload["new_value"],
        )

    if opcode is OpCode.INSERT:
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
