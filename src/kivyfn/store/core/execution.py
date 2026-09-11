# src/kivyfn/store/core/execution.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from kivyfn.store.core.compiler import Compiler
from kivyfn.store.core.constraintguard import ConstraintGuard
from kivyfn.store.core.delta import Delta
from kivyfn.store.core.instruction import Instruction
from kivyfn.store.error import (
    StoreErrorContext,
    StoreErrorStage,
    StoreOperationError,
)
from kivyfn.store.events.event import StoreCommit

if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.core.operations import Operation
    from kivyfn.store.node import Node


@dataclass(slots=True)
class ExecutionResult:
    success: bool
    stage: StoreErrorStage
    compiled: tuple[Operation, ...] = ()
    error: StoreOperationError | None = None
    commit: StoreCommit | None = None
    touched_nodes: frozenset[Node] = field(default_factory=frozenset)


class Execution:
    """Compile, validate, and apply one batch of typed instructions."""

    def __init__(
        self,
        engine: Engine,
        instructions: tuple[Instruction, ...],
        *,
        operation_id: str | None = None,
    ) -> None:
        self.engine = engine
        self.instructions = instructions
        self.operation_id = operation_id

    def execute(self) -> ExecutionResult:
        compiler = Compiler(self.engine, self.instructions)

        try:
            compiled = compiler.compile()
        except StoreOperationError as exc:
            self._attach_stage(exc, StoreErrorStage.COMPILE)

            return ExecutionResult(
                success=False,
                stage=StoreErrorStage.COMPILE,
                compiled=tuple(compiler.operations),
                error=exc,
            )

        try:
            delta = Delta(compiled)
        except StoreOperationError as exc:
            self._attach_stage(exc, StoreErrorStage.DELTA)
            return ExecutionResult(
                success=False,
                stage=StoreErrorStage.DELTA,
                compiled=tuple(compiled),
                error=exc,
            )

        try:
            ConstraintGuard(self.engine).validate(delta)
        except StoreOperationError as exc:
            self._attach_stage(exc, StoreErrorStage.VALIDATE)

            return ExecutionResult(
                success=False,
                stage=StoreErrorStage.VALIDATE,
                compiled=tuple(compiled),
                error=exc,
            )

        events = tuple(operation.execute() for operation in compiled)

        return ExecutionResult(
            success=True,
            stage=StoreErrorStage.EXECUTE,
            compiled=tuple(compiled),
            commit=StoreCommit(events=events, operation_id=self.operation_id),
            touched_nodes=delta.touched_nodes,
        )

    def _attach_stage(
        self, error: StoreOperationError, stage: StoreErrorStage
    ) -> None:
        if error.context is None:
            error.context = StoreErrorContext(stage=stage)
            return

        if error.context.stage is not None:
            return

        error.context = replace(error.context, stage=stage)
