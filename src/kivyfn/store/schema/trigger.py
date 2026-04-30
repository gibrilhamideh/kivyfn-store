from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Callable, TypeVar, Protocol, Any, TYPE_CHECKING

from ..constants import InstructionOpCode

if TYPE_CHECKING:
    from ..core.instruction import Instruction
    from ..core.transaction import Transaction
    from ..node import Node


@dataclass(frozen=True)
class Trigger:
    opcode: InstructionOpCode
    field: Optional[str]
    fn: Callable  # fn(node, action, exec)

    def matches(self, opcode: InstructionOpCode, key: Optional[str]) -> bool:
        if self.opcode is not opcode:
            return False

        if self.field is not None and self.field != key:
            return False

        return True


class TriggerFunc(Protocol):
    def __call__(
        self, node: Node, action: Instruction, exec_fn: Transaction
    ) -> Any: ...


F = TypeVar("F", bound=TriggerFunc)


def trigger(opcode: InstructionOpCode, *, field: Optional[str] = None):
    def decorator(fn: F) -> F:
        # 1. Attach metadata to the raw function
        if not hasattr(fn, "_trigger_meta"):
            fn._trigger_meta = []  # type: ignore

        fn._trigger_meta.append(Trigger(opcode=opcode, field=field, fn=fn))  # type: ignore

        # 2. Convert to staticmethod and CAST back to F
        # This trick fools the type checker into thinking it's still a
        # regular function (so no 'self' error) while actually making
        # it a staticmethod at runtime.
        return fn

    return decorator
