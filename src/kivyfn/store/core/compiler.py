# kivyfn/store/core/compiler.py
from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING

from kivyfn.store.core.instruction import Instruction
from kivyfn.store.core.operations import (
    InsertOperation,
    Operation,
    RemoveOperation,
    SetOperation,
    SetRowOperation,
)
from kivyfn.store.error import (
    ConstraintErrorDetail,
    ForeignKeyRestrictedError,
    InstructionErrorDetail,
    InvalidInstructionError,
    InvalidOperationError,
    StoreErrorContext,
    StoreErrorStage,
    StoreOperationError,
)
from kivyfn.store.operations import (
    OpCode,
    OperationSource,
)
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.policy import ForeignKeyPolicy
from kivyfn.store.transaction import Transaction


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


class Compiler:
    def __init__(
        self,
        engine: Engine,
        instructions: list[Instruction],
    ) -> None:
        self.engine = engine
        self.pending = deque(instructions)
        self.operations: list[Operation] = []

    def compile(self) -> list[Operation]:
        while self.pending:
            instruction = self.pending.popleft()
            node: Node | None = None

            try:
                node = self.engine.get_node_by_address(
                    instruction.address,
                )

                operation, instruction = self._compile_action(
                    node,
                    instruction,
                )

                self.operations.append(
                    operation,
                )

                derived: list[Instruction] = []

                derived.extend(
                    self._collect_triggers(
                        operation,
                    )
                )

                derived.extend(
                    self._collect_policies(
                        node,
                        instruction,
                    )
                )

                for new_instruction in derived:
                    self.pending.append(
                        new_instruction,
                    )

            except StoreOperationError as exc:
                self._attach_context(
                    exc,
                    instruction=instruction,
                    node=node,
                )

                raise

        return self.operations

    # ============================= #
    # Action Compiler
    # ============================= #

    def _compile_action(
        self,
        node: Node,
        instruction: Instruction,
    ) -> tuple[Operation, Instruction]:
        if instruction.opcode is OpCode.SET:
            return self._compile_set(
                node,
                instruction,
            )

        if instruction.opcode is OpCode.SETROW:
            return self._compile_setrow(
                node,
                instruction,
            )

        if instruction.opcode is OpCode.INSERT:
            return self._compile_insert(
                node,
                instruction,
            )

        if instruction.opcode is OpCode.REMOVE:
            return self._compile_remove(
                node,
                instruction,
            )

        raise InvalidInstructionError(
            f"Unknown instruction opcode {instruction.opcode!r}",
            detail=self._instruction_detail(
                instruction,
            ),
        )

    # ============================= #
    # Operation Compiler
    # ============================= #

    def _compile_set(
        self,
        node: Node,
        instruction: Instruction,
    ) -> tuple[Operation, Instruction]:
        schema = node.schema

        if schema.is_many():
            raise InvalidOperationError(
                "SET is only valid on a single schema",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        if instruction.key is None:
            raise InvalidInstructionError(
                "SET requires a key",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        schema.challenge_field(
            instruction.key,
            instruction.value,
        )

        return (
            SetOperation(
                node,
                instruction,
            ),
            instruction,
        )

    def _compile_setrow(
        self,
        node: Node,
        instruction: Instruction,
    ) -> tuple[Operation, Instruction]:
        schema = node.schema

        if not schema.is_many():
            raise InvalidOperationError(
                "SETROW is only valid on a many schema",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        if instruction.id is None:
            raise InvalidInstructionError(
                "SETROW requires an id",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        if instruction.key is None:
            raise InvalidInstructionError(
                "SETROW requires a key",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        schema.challenge_field(
            instruction.key,
            instruction.value,
        )

        return (
            SetRowOperation(
                node,
                instruction,
            ),
            instruction,
        )

    def _compile_insert(
        self,
        node: Node,
        instruction: Instruction,
    ) -> tuple[Operation, Instruction]:
        schema = node.schema

        if not schema.is_many():
            raise InvalidOperationError(
                "INSERT is only valid on a many schema",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        if instruction.id is None:
            raise InvalidInstructionError(
                "INSERT requires an id",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        if not isinstance(instruction.value, dict):
            raise InvalidInstructionError(
                "INSERT requires a dictionary value",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        built = node._build_default(
            instruction.value,
        )

        instruction = instruction.copy(
            value=built,
        )

        return (
            InsertOperation(
                node,
                instruction,
            ),
            instruction,
        )

    def _compile_remove(
        self,
        node: Node,
        instruction: Instruction,
    ) -> tuple[Operation, Instruction]:
        schema = node.schema

        if not schema.is_many():
            raise InvalidOperationError(
                "REMOVE is only valid on a many schema",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        if instruction.id is None:
            raise InvalidInstructionError(
                "REMOVE requires an id",
                detail=self._instruction_detail(
                    instruction,
                ),
            )

        return (
            RemoveOperation(
                node,
                instruction,
            ),
            instruction,
        )

    # ============================= #
    # Trigger Executions
    # ============================= #

    def _collect_triggers(
        self,
        operation: Operation,
    ) -> list[Instruction]:
        derived: list[Instruction] = []

        node = operation.node
        instruction = operation.instruction

        context = TriggerContext(
            engine=self.engine,
            operation=operation,
            operations=self.operations,
        )

        for trigger in node.schema.triggers():
            if not trigger.matches(
                instruction.opcode,
                instruction.key,
            ):
                continue

            builder = Transaction()

            trigger.fn(
                builder,
                context,
            )

            instructions = Instruction.construct(
                builder.commands,
                source=OperationSource.TRIGGER,
            )

            derived.extend(
                instructions,
            )

        return derived

    # ============================= #
    # Policy Executions
    # ============================= #

    def _collect_policies(
        self,
        node: Node,
        instruction: Instruction,
    ) -> list[Instruction]:
        if instruction.opcode is not OpCode.REMOVE:
            return []

        derived: list[Instruction] = []

        target_schema = node.schema.name
        removed_id = instruction.id

        foreign_keys = self.engine.metadata.referenced_by(
            target_schema,
        )

        if not foreign_keys:
            return []

        for foreign_key in foreign_keys:
            referencing_node = self.engine.get_node_by_schema(
                foreign_key.schema,
            )

            ids = referencing_node.lookup(
                foreign_key.field,
                removed_id,
            )

            if not ids:
                continue

            if foreign_key.on_delete is ForeignKeyPolicy.RESTRICT:
                raise ForeignKeyRestrictedError(
                    (
                        f"Cannot delete {target_schema} "
                        f"{removed_id!r} because it is referenced by "
                        f"{foreign_key.schema}.{foreign_key.field}"
                    ),
                    detail=ConstraintErrorDetail(
                        schema=target_schema,
                        row_id=removed_id,
                        referencing_schema=foreign_key.schema,
                        referencing_field=foreign_key.field,
                        referencing_row_ids=tuple(ids),
                    ),
                )

            if foreign_key.on_delete is ForeignKeyPolicy.CASCADE:
                for row_id in ids:
                    derived.append(
                        Instruction.remove(
                            referencing_node.address,
                            row_id,
                            source=OperationSource.POLICY,
                        )
                    )

        return derived

    # ============================= #
    # Error Helpers
    # ============================= #

    def _instruction_detail(
        self,
        instruction: Instruction,
    ) -> InstructionErrorDetail:
        return InstructionErrorDetail(
            opcode=instruction.opcode.name,
            address=instruction.address.parts,
            row_id=instruction.id,
            field=instruction.key,
            value=instruction.value,
            source=instruction.source.value,
        )

    def _attach_context(
        self,
        error: StoreOperationError,
        *,
        instruction: Instruction,
        node: Node | None,
    ) -> None:
        schema = (
            node.schema.name
            if node is not None
            else None
        )

        context = StoreErrorContext(
            stage=StoreErrorStage.COMPILE,
            schema=schema,
            address=instruction.address.parts,
            opcode=instruction.opcode.name,
            source=instruction.source.value,
            row_id=instruction.id,
            field=instruction.key,
            value=instruction.value,
        )

        if error.context is None:
            error.context = context
            return

        error.context = replace(
            error.context,
            stage=(
                error.context.stage
                or context.stage
            ),
            schema=(
                error.context.schema
                or context.schema
            ),
            address=(
                error.context.address
                or context.address
            ),
            opcode=(
                error.context.opcode
                or context.opcode
            ),
            source=(
                error.context.source
                or context.source
            ),
            row_id=(
                error.context.row_id
                or context.row_id
            ),
            field=(
                error.context.field
                or context.field
            ),
            value=(
                error.context.value
                if error.context.value is not None
                else context.value
            ),
        )
