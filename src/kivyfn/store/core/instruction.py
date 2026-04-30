from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional

from ..address import Address
from ..constants import InstructionOpCode, InstructionSource


@dataclass(frozen=True, kw_only=True)
class Instruction:
    """
    Primitive immutable mutation descriptor.

    Action represents a single atomic structural mutation.
    Higher-level concepts like 'merge' must expand into
    multiple primitive Actions before compilation.
    """

    opcode: InstructionOpCode
    address: Address
    value: Optional[Any] = None
    id: Optional[str] = None
    key: Optional[str] = None
    source: InstructionSource

    # --------------------
    # Representation
    # --------------------
    def __repr__(self) -> str:
        return (
            f"Instruction(opcode={self.opcode.value!r}, "
            f"address={self.address!r}, "
            f"value={self.value!r}, "
            f"id={self.id!r}, "
            f"key={self.key!r}, "
            f"source={self.source!r})"
        )

    # --------------------
    # Copy
    # --------------------
    def copy(self, **overrides) -> Instruction:
        return Instruction(
            opcode=overrides.get("opcode", self.opcode),
            address=overrides.get("address", self.address),
            value=overrides.get("value", self.value),
            id=overrides.get("id", self.id),
            key=overrides.get("key", self.key),
            source=overrides.get("source", self.source),
        )

    # --------------------
    # Serialization
    # --------------------
    def dump(self) -> dict:
        return {
            "opcode": self.opcode.value,
            "address": list(self.address),
            "value": self.value,
            "id": self.id,
            "key": self.key,
            "source": self.source,
        }

    # ============================================================
    # Factories (primitive only)
    # ============================================================

    @staticmethod
    def set(
        address: Address, key: str, value: Any, source=InstructionSource.COMMAND
    ) -> Instruction:
        """
        Primitive single-schema field mutation.
        """
        return Instruction(
            opcode=InstructionOpCode.SET,
            address=address,
            key=key,
            value=value,
            source=source,
        )

    @staticmethod
    def setrow(
        address: Address,
        id: str,
        key: str,
        value: Any,
        source: InstructionSource = InstructionSource.COMMAND,
    ) -> Instruction:
        """
        Primitive collection row field mutation.
        """
        return Instruction(
            opcode=InstructionOpCode.SETROW,
            address=address,
            id=id,
            key=key,
            value=value,
            source=source,
        )

    @staticmethod
    def insert(
        address: Address,
        id: str,
        value: dict,
        source: InstructionSource = InstructionSource.COMMAND,
    ) -> Instruction:
        """
        Primitive row insertion into a collection node.

        Parameters:
            address : Address resolving to a many-schema node
            id      : Unique row identifier (must be provided)
            value   : Dictionary representing row fields
            source  : Origin of the action

        Notes:
            ID generation must occur before Action creation.
            This method does not generate identifiers.
        """
        return Instruction(
            opcode=InstructionOpCode.INSERT,
            address=address,
            id=id,
            value=value,
            source=source,
        )

    @staticmethod
    def remove(
        address: Address, id: str, source: InstructionSource = InstructionSource.COMMAND
    ) -> Instruction:
        """
        Primitive row removal from collection.
        """
        return Instruction(
            opcode=InstructionOpCode.REMOVE,
            address=address,
            id=id,
            source=source,
        )
