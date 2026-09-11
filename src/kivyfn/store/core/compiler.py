# src/kivyfn/store/core/compiler.py
from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any, assert_never

from kivyfn.store.core.instruction import (
    InsertInstruction,
    Instruction,
    RemoveInstruction,
    SetInstruction,
    SetRowInstruction,
    instructions_from_payload,
)
from kivyfn.store.core.operations import (
    InsertOperation,
    Operation,
    RemoveOperation,
    SetOperation,
    SetRowOperation,
)
from kivyfn.store.error import (
    InstructionErrorDetail,
    InvalidOperationError,
    StoreErrorContext,
    StoreErrorStage,
    StoreOperationError,
)
from kivyfn.store.operations import OperationSource
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.policy import ForeignKeyPolicy
from kivyfn.store.transaction import Transaction

if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


class Compiler:
    def __init__(
        self, engine: Engine, instructions: tuple[Instruction, ...]
    ) -> None:
        self.engine = engine
        self.pending = deque(instructions)
        self.operations: list[Operation] = []

    def compile(self) -> list[Operation]:
        while self.pending:
            instruction = self.pending.popleft()
            node: Node | None = None

            try:
                node = self.engine.get_node_by_address(instruction.address)

                operation = self._compile_action(node, instruction)

                if self._is_noop(operation):
                    continue

                self.operations.append(operation)

                self.pending.extend(self._collect_triggers(operation))
                self.pending.extend(self._collect_policies(operation))

            except StoreOperationError as exc:
                self._attach_context(exc, instruction=instruction, node=node)
                raise

        return self.operations

    # -------------------------------------------------
    # Operation compilation
    # -------------------------------------------------

    def _compile_action(
        self, node: Node, instruction: Instruction
    ) -> Operation:
        if isinstance(instruction, SetInstruction):
            return self._compile_set(node, instruction)

        if isinstance(instruction, SetRowInstruction):
            return self._compile_setrow(node, instruction)

        if isinstance(instruction, InsertInstruction):
            return self._compile_insert(node, instruction)

        if isinstance(instruction, RemoveInstruction):
            return self._compile_remove(node, instruction)

        assert_never(instruction)

    def _compile_set(
        self, node: Node, instruction: SetInstruction
    ) -> SetOperation:
        schema = node.schema

        if not isinstance(schema, SingleSchema):
            raise InvalidOperationError(
                "SET is only valid on a single schema",
                detail=self._instruction_detail(instruction),
            )

        schema.challenge_field(instruction.key, instruction.value)

        return SetOperation(node, instruction)

    def _compile_setrow(
        self, node: Node, instruction: SetRowInstruction
    ) -> SetRowOperation:
        schema = node.schema

        if not isinstance(schema, ManySchema):
            raise InvalidOperationError(
                "SETROW is only valid on a many schema",
                detail=self._instruction_detail(instruction),
            )

        schema.challenge_field(instruction.key, instruction.value)

        return SetRowOperation(node, instruction)

    def _compile_insert(
        self, node: Node, instruction: InsertInstruction
    ) -> InsertOperation:
        schema = node.schema

        if not isinstance(schema, ManySchema):
            raise InvalidOperationError(
                "INSERT is only valid on a many schema",
                detail=self._instruction_detail(instruction),
            )

        built = node.build_row(instruction.value)

        resolved = replace(instruction, value=built)

        return InsertOperation(node, resolved)

    def _compile_remove(
        self, node: Node, instruction: RemoveInstruction
    ) -> RemoveOperation:
        schema = node.schema

        if not isinstance(schema, ManySchema):
            raise InvalidOperationError(
                "REMOVE is only valid on a many schema",
                detail=self._instruction_detail(instruction),
            )

        return RemoveOperation(node, instruction)

    # -------------------------------------------------
    # Actual-change filtering
    # -------------------------------------------------

    def _is_noop(self, operation: Operation) -> bool:
        if isinstance(operation, SetOperation):
            current = operation.node.value.get(operation.key)

            for previous in reversed(self.operations):
                if (
                    isinstance(previous, SetOperation)
                    and previous.node is operation.node
                    and previous.key == operation.key
                ):
                    current = previous.new_value
                    break

            return current == operation.new_value

        if not isinstance(operation, SetRowOperation):
            return False

        missing = object()
        current: object = missing

        for previous in reversed(self.operations):
            if previous.node is not operation.node:
                continue

            if isinstance(previous, RemoveOperation):
                if previous.id == operation.id:
                    return False

            if isinstance(previous, InsertOperation):
                if previous.id == operation.id:
                    current = previous.value.get(operation.key, missing)
                    break

            if isinstance(previous, SetRowOperation):
                if (
                    previous.id == operation.id
                    and previous.key == operation.key
                ):
                    current = previous.new_value
                    break

        if current is missing:
            row = operation.node.value.get(operation.id)

            if not isinstance(row, dict):
                return False

            current = row.get(operation.key, missing)

        return current is not missing and current == operation.new_value

    # -------------------------------------------------
    # Triggers
    # -------------------------------------------------

    def _collect_triggers(self, operation: Operation) -> list[Instruction]:
        node = operation.node
        instruction = operation.instruction

        context = TriggerContext(
            engine=self.engine, operation=operation, operations=self.operations
        )

        derived: list[Instruction] = []

        for trigger in node.schema.triggers():
            if not trigger.matches(
                instruction.opcode, _instruction_key(instruction)
            ):
                continue

            builder = Transaction()

            trigger.fn(builder, context)

            derived.extend(
                instructions_from_payload(
                    builder.commands, OperationSource.TRIGGER
                )
            )

        return derived

    # -------------------------------------------------
    # Foreign-key policies
    # -------------------------------------------------

    def _collect_policies(self, operation: Operation) -> list[Instruction]:
        if not isinstance(operation, RemoveOperation):
            return []

        target_schema = operation.node.schema.name
        removed_id = operation.id

        derived: list[Instruction] = []

        for foreign_key in self.engine.metadata.referenced_by(target_schema):
            if foreign_key.on_delete is not ForeignKeyPolicy.CASCADE:
                continue

            referencing_node = self.engine.get_node_by_schema(
                foreign_key.schema
            )

            row_ids = referencing_node.lookup_all(
                foreign_key.field, removed_id
            )

            derived.extend(
                RemoveInstruction(
                    address=referencing_node.address,
                    id=row_id,
                    source=OperationSource.POLICY,
                )
                for row_id in row_ids
            )

        return derived

    # -------------------------------------------------
    # Error context
    # -------------------------------------------------

    def _instruction_detail(
        self, instruction: Instruction
    ) -> InstructionErrorDetail:
        row_id, field, value = _instruction_values(instruction)

        return InstructionErrorDetail(
            opcode=instruction.opcode.name,
            address=instruction.address.parts,
            row_id=row_id,
            field=field,
            value=value,
            source=instruction.source.value,
        )

    def _attach_context(
        self,
        error: StoreOperationError,
        *,
        instruction: Instruction,
        node: Node | None,
    ) -> None:
        row_id, field, value = _instruction_values(instruction)

        context = StoreErrorContext(
            stage=StoreErrorStage.COMPILE,
            schema=(node.schema.name if node is not None else None),
            address=instruction.address.parts,
            opcode=instruction.opcode.name,
            source=instruction.source.value,
            row_id=row_id,
            field=field,
            value=value,
        )

        if error.context is None:
            error.context = context
            return

        error.context = replace(
            error.context,
            stage=(error.context.stage or context.stage),
            schema=(error.context.schema or context.schema),
            address=(error.context.address or context.address),
            opcode=(error.context.opcode or context.opcode),
            source=(error.context.source or context.source),
            row_id=(error.context.row_id or context.row_id),
            field=(error.context.field or context.field),
            value=(
                error.context.value
                if error.context.value is not None
                else context.value
            ),
        )


def _instruction_key(instruction: Instruction) -> str | None:
    if isinstance(instruction, (SetInstruction, SetRowInstruction)):
        return instruction.key

    return None


def _instruction_values(
    instruction: Instruction,
) -> tuple[str | None, str | None, Any]:
    if isinstance(instruction, SetInstruction):
        return (None, instruction.key, instruction.value)

    if isinstance(instruction, SetRowInstruction):
        return (instruction.id, instruction.key, instruction.value)

    if isinstance(instruction, InsertInstruction):
        return (instruction.id, None, instruction.value)

    if isinstance(instruction, RemoveInstruction):
        return (instruction.id, None, None)

    assert_never(instruction)
