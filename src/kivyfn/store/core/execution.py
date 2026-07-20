# kivyfn/store/core/execution.py
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

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
from kivyfn.store.error import (
    StoreErrorContext,
    StoreErrorStage,
    StoreOperationError,
    encode_failure,
)
from kivyfn.store.operations import OperationSource


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
        error: StoreOperationError | None = None,
        inserted: list | None = None,
        removed: list | None = None,
        updated: list | None = None,
        events: list[StoreEvent] | None = None,
        touched_nodes: set[Node] | None = None,
    ) -> None:
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
        data: dict[str, Any] = {
            "success": self.success,
            "stage": self.stage,
            "inserted": self.inserted,
            "removed": self.removed,
            "updated": self.updated,
        }

        if self.error is not None:
            data["error"] = encode_failure(
                self.error.failure(),
            )

        return data


class Execution:
    def __init__(
        self,
        engine: Engine,
        commands: list,
    ) -> None:
        self.engine = engine
        self.commands = commands

    def execute(self) -> ExecutionResult:
        instructions = Instruction.construct(
            self.commands,
            OperationSource.COMMAND,
        )

        compiler = Compiler(
            self.engine,
            instructions,
        )

        try:
            compiled_ops = compiler.compile()
        except StoreOperationError as exc:
            self._attach_stage(
                exc,
                StoreErrorStage.COMPILE,
            )

            return ExecutionResult(
                success=False,
                stage=StoreErrorStage.COMPILE.value,
                compiled=compiler.operations,
                error=exc,
            )

        try:
            delta = Delta(
                self.engine,
                compiled_ops,
            )
        except StoreOperationError as exc:
            self._attach_stage(
                exc,
                StoreErrorStage.DELTA,
            )

            return ExecutionResult(
                success=False,
                stage=StoreErrorStage.DELTA.value,
                compiled=compiled_ops,
                error=exc,
            )

        try:
            ConstraintGuard(
                self.engine,
            ).validate(
                delta,
            )
        except StoreOperationError as exc:
            self._attach_stage(
                exc,
                StoreErrorStage.VALIDATE,
            )

            return ExecutionResult(
                success=False,
                stage=StoreErrorStage.VALIDATE.value,
                compiled=compiled_ops,
                error=exc,
            )

        events: list[StoreEvent] = []
        inserted: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []

        for operation in compiled_ops:
            event = operation.execute()

            if isinstance(operation, InsertOperation):
                inserted.append(
                    {
                        "address": operation.address.to_list(),
                        "id": operation.id,
                        "source": operation.source.value,
                    }
                )

            elif isinstance(operation, RemoveOperation):
                removed.append(
                    {
                        "address": operation.address.to_list(),
                        "id": operation.id,
                        "source": operation.source.value,
                    }
                )

            elif isinstance(
                operation,
                (
                    SetOperation,
                    SetRowOperation,
                ),
            ):
                updated.append(
                    {
                        "address": operation.address.to_list(),
                        "source": operation.source.value,
                    }
                )

            if event is not None:
                events.append(
                    event,
                )

        return ExecutionResult(
            success=True,
            stage=StoreErrorStage.EXECUTE.value,
            compiled=compiled_ops,
            inserted=inserted,
            removed=removed,
            updated=updated,
            events=events,
            touched_nodes=delta.touched_nodes,
        )

    def _attach_stage(
        self,
        error: StoreOperationError,
        stage: StoreErrorStage,
    ) -> None:
        if error.context is None:
            error.context = StoreErrorContext(
                stage=stage,
            )
            return

        if error.context.stage is not None:
            return

        error.context = replace(
            error.context,
            stage=stage,
        )
