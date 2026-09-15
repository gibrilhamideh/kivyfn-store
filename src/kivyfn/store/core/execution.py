# src/kivyfn/store/core/execution.py
from __future__ import annotations

from collections.abc import Callable
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
from kivyfn.store.node.node import NodeState

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
        transaction_id: str | None = None,
        validate: Callable[[Delta], None] | None = None,
    ) -> None:
        self.engine = engine
        self.instructions = instructions
        self.transaction_id = transaction_id
        self.validate = validate
        self.prepared: dict[Node, NodeState] = {}

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

        delta = compiler.delta

        try:
            ConstraintGuard(self.engine).validate(delta)
            if self.validate is not None:
                self.validate(delta)
            self.prepared = {
                node: node.prepare(data) for node, data in delta.values.items()
            }
        except StoreOperationError as exc:
            self._attach_stage(exc, StoreErrorStage.VALIDATE)

            return ExecutionResult(
                success=False,
                stage=StoreErrorStage.VALIDATE,
                compiled=tuple(compiled),
                error=exc,
            )

        events = tuple(delta.events)

        return ExecutionResult(
            success=True,
            stage=StoreErrorStage.EXECUTE,
            compiled=tuple(compiled),
            commit=StoreCommit(
                events=events, transaction_id=self.transaction_id
            ),
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
