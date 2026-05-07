from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.address import Address


@dataclass(frozen=True, slots=True)
class StoreEvent:
    opcode: InstructionOpCode
    address: Address

    id: Optional[str] = None
    key: Optional[str] = None
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None

    def payload(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode.value,
            "address": self.address.to_list(),
            "id": self.id,
            "key": self.key,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }

    def copy_with(self, **changes: Any) -> StoreEvent:
        return replace(self, **changes)
