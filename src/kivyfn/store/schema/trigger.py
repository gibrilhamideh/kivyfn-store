# kivyfn/store/schema/trigger.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, TYPE_CHECKING, TypeVar

from kivyfn.store.constants import InstructionOpCode

if TYPE_CHECKING:
    from kivyfn.store.schema import Schema
    from kivyfn.store.schema.context import TriggerContext
    from kivyfn.store.transaction import TransactionBuilder


@dataclass(frozen=True)
class Trigger:
    opcode: InstructionOpCode
    field: Optional[str]
    fn: Callable[..., Any]

    def matches(self, opcode: InstructionOpCode, key: Optional[str]) -> bool:
        if self.opcode is not opcode:
            return False

        if self.field is not None and self.field != key:
            return False

        return True


class TriggerFunc(Protocol):
    def __call__(
        self,
        schema: Schema,
        builder: TransactionBuilder,
        context: TriggerContext,
    ) -> Any: ...


F = TypeVar("F", bound=Callable[..., Any])


def trigger(opcode: InstructionOpCode, *, field: Optional[str] = None):
    def decorator(fn: F) -> F:
        trigger_meta = getattr(fn, "_trigger_meta", None)

        if trigger_meta is None:
            trigger_meta = []
            setattr(fn, "_trigger_meta", trigger_meta)

        trigger_meta.append(
            Trigger(
                opcode=opcode,
                field=field,
                fn=fn,
            )
        )

        return fn

    return decorator
