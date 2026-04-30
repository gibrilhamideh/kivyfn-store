from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from ..constants import InstructionOpCode
from ..address import Address


@dataclass(frozen=True, slots=True)
class NodeEvent:
    """
    Immutable event emitted after a committed operation.

    Represents a single structural mutation that already
    passed validation and has been executed.

    This object is safe to expose to external consumers
    (UI layer, observers, logging systems).
    """

    opcode: InstructionOpCode
    address: Address

    # Row-level operations
    id: Optional[str] = None

    # Field-level operations
    key: Optional[str] = None

    # Value transitions (for SET / SETROW)
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None

    # ----------------------------
    # Convenience flags
    # ----------------------------

    @property
    def is_set(self) -> bool:
        return self.opcode is InstructionOpCode.SET

    @property
    def is_setrow(self) -> bool:
        return self.opcode is InstructionOpCode.SETROW

    @property
    def is_insert(self) -> bool:
        return self.opcode is InstructionOpCode.INSERT

    @property
    def is_remove(self) -> bool:
        return self.opcode is InstructionOpCode.REMOVE

    # ----------------------------
    # Debug representation
    # ----------------------------

    def __repr__(self) -> str:
        base = f"opcode={self.opcode.value!r}, address={self.address!r}"

        if self.id is not None:
            base += f", id={self.id!r}"

        if self.key is not None:
            base += f", key={self.key!r}"

        if self.old_value is not None or self.new_value is not None:
            base += f", old={self.old_value!r}, new={self.new_value!r}"

        return f"NodeEvent({base})"

    def copy_with(self, **changes: Any) -> NodeEvent:
        """
        Creates a new NodeEvent with modified fields.
        Useful for translating IDs and Addresses across foreign keys.
        """
        return replace(self, **changes)
