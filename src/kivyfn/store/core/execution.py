from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.store.operations import OperationSource
from kivyfn.store.core.compiler import Compiler
from kivyfn.store.core.constraintguard import ConstraintGuard
from kivyfn.store.core.delta import Delta
from kivyfn.store.core.instruction import Instruction
from kivyfn.store.core.operations import (
    InsertOperation,
    RemoveOperation,
    SetOperation,
    SetRowOperation,
)
from kivyfn.store.errors.store import CompilerError, ConstraintError, StoreError

if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.events.event import StoreEvent
    from kivyfn.store.node import Node


class ExecutionResult:
    def __init__(
        self,
        *,
        success: bool,
        stage: str,
        compiled: list | None = None,
        error: StoreError | None = None,
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
            data["error"] = self.error.payload()

        return data


class Execution:
    def __init__(self, engine: Engine, commands: list):
        self.engine = engine
        self.commands = commands

    def execute(self) -> ExecutionResult:
        instructions = Instruction.construct(
            self.commands,
            OperationSource.COMMAND,
        )

        compiler = Compiler(self.engine, instructions)

        try:
            compiled_ops = compiler.compile()
        except CompilerError as exc:
            return ExecutionResult(
                success=False,
                stage="compile",
                compiled=compiler.operations,
                error=exc,
            )

        try:
            delta = Delta(self.engine, compiled_ops)
        except CompilerError as exc:
            return ExecutionResult(
                success=False,
                stage="delta",
                compiled=compiled_ops,
                error=exc,
            )

        try:
            ConstraintGuard(self.engine).validate(delta)
        except ConstraintError as exc:
            return ExecutionResult(
                success=False,
                stage="validate",
                compiled=compiled_ops,
                error=exc,
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
