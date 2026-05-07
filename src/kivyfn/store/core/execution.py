from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.store.core.compiler import Compiler
from kivyfn.store.core.delta import Delta
from kivyfn.store.core.constraintguard import ConstraintGuard
from kivyfn.store.core.instruction import Instruction
from kivyfn.store.constants import InstructionSource
from kivyfn.store.core.operations import (
    SetOperation,
    SetRowOperation,
    InsertOperation,
    RemoveOperation,
)


from kivyfn.store.errors import ConstraintError, CompilerError

if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.core.instruction import Instruction
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.node import Node


class ExecutionResult:
    def __init__(
        self,
        *,
        success: bool,
        stage: str,
        compiled: list | None = None,
        error: Exception | dict | None = None,
        inserted: list | None = None,
        removed: list | None = None,
        updated: list | None = None,
        events: list[StoreEvent] | None = None,
        touched_nodes: set[Node] | None = None,
    ):
        self.success = success
        self.stage = stage
        self.compiled = compiled or []
        self.error = error
        self.inserted = inserted or []
        self.removed = removed or []
        self.updated = updated or []
        self.events = events or []
        self.touched_nodes = touched_nodes or set()

    def payload(self) -> dict[str, Any]:
        data = {
            "success": self.success,
            "stage": self.stage,
            "inserted": self.inserted,
            "removed": self.removed,
            "updated": self.updated,
        }

        if self.error is not None:
            if isinstance(self.error, dict):
                data["error"] = self.error
            else:
                data["error"] = {
                    "type": self.error.__class__.__name__,
                    "message": str(self.error),
                }

        return data

class Execution:
    """
    Developer-facing mutation builder.

    Execution collects high-level mutation commands and converts them
    into low-level Action objects to be compiled by the store.

    Commands are intentionally separated between:

    - Single-schema (object) mutations
    - Many-schema (collection) mutations

    Each method encodes structural intent explicitly.
    """

    def __init__(
        self,
        engine: Engine,
        commands: list
    ):
        self.engine = engine
        self.commands = commands

    # ----------------------------
    # Execute
    # ----------------------------
    def execute(self) -> ExecutionResult:
        instructions = Instruction.construct(
            self.commands,
            InstructionSource.COMMAND,
        )

        compiler = Compiler(self.engine, instructions)

        try:
            compiled_ops = compiler.compile()
        except CompilerError as e:
            return ExecutionResult(
                success=False,
                stage="compile",
                compiled=compiler.operations,
                error=e,
            )

        try:
            delta = Delta(self.engine, compiled_ops)
        except CompilerError as e:
            return ExecutionResult(
                success=False,
                stage="compile",
                compiled=compiled_ops,
                error=e,
            )

        try:
            ConstraintGuard(self.engine).validate(delta)
        except ConstraintError as e:
            return ExecutionResult(
                success=False,
                stage="validate",
                compiled=compiled_ops,
                error=e,
            )

        events: list[StoreEvent] = []
        inserted = []
        removed = []
        updated = []

        for op in compiled_ops:
            event = op.execute()

            if isinstance(op, InsertOperation):
                inserted.append(
                    {
                        "address": op.address.to_list(),
                        "id": op.id,
                        "source": op.source.value,
                    }
                )

            elif isinstance(op, RemoveOperation):
                removed.append(
                    {
                        "address": op.address.to_list(),
                        "id": op.id,
                        "source": op.source.value,
                    }
                )

            elif isinstance(op, (SetOperation, SetRowOperation)):
                updated.append(
                    {
                        "address": op.address.to_list(),
                        "source": op.source.value,
                    }
                )

            if event is not None:
                events.append(event)

        return ExecutionResult(
            success=True,
            stage="execute",
            compiled=compiled_ops,
            inserted=inserted,
            removed=removed,
            updated=updated,
            events=events,
            touched_nodes=delta.touched_nodes,
        )
