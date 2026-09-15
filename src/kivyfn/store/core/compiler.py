# src/kivyfn/store/core/compiler.py
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, assert_never

from kivyfn.store.core.delta import Delta
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


@dataclass(frozen=True, slots=True)
class _CascadeBoundary:
    """Expand a deletion after its trigger commands have been compiled."""

    node: Node
    row_id: str


class Compiler:
    MAX_OPERATIONS = 10_000

    def __init__(
        self, engine: Engine, instructions: tuple[Instruction, ...]
    ) -> None:
        self.engine = engine
        self.pending: deque[Instruction | _CascadeBoundary] = deque(
            instructions
        )
        self.operations: list[Operation] = []
        self.delta = Delta()
        self._policy_deleted: set[tuple[Node, str]] = set()

    def compile(self) -> list[Operation]:
        while self.pending:
            if len(self.operations) >= self.MAX_OPERATIONS:
                raise InvalidOperationError(
                    f"Transaction exceeds the {self.MAX_OPERATIONS}-operation "
                    "expansion limit"
                )
            instruction = self.pending.popleft()
            if isinstance(instruction, _CascadeBoundary):
                policies = self._collect_policies(
                    instruction.node, instruction.row_id
                )
                self.pending.extendleft(reversed(policies))
                continue
            node: Node | None = None

            try:
                node = self.engine.get_node_by_address(instruction.address)

                if (
                    isinstance(instruction, RemoveInstruction)
                    and not self.delta.exists(node, instruction.id)
                    and (
                        instruction.source is OperationSource.POLICY
                        or (node, instruction.id) in self._policy_deleted
                    )
                ):
                    continue

                operation = self._compile_action(node, instruction)

                if self._is_noop(operation):
                    continue

                self.delta.add(operation)
                self.operations.append(operation)

                if isinstance(operation, RemoveOperation):
                    if instruction.source is OperationSource.POLICY:
                        self._policy_deleted.add((node, operation.id))
                    self.pending.appendleft(
                        _CascadeBoundary(node, operation.id)
                    )
                elif isinstance(operation, InsertOperation):
                    self._policy_deleted.discard((node, operation.id))

                self.pending.extendleft(
                    reversed(self._collect_triggers(operation))
                )

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

        for key in instruction.value.keys() - schema.field_names():
            schema.get_field(key)
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
        data = self.delta.data(operation.node)
        if isinstance(operation, SetOperation):
            return data.get(operation.key) == operation.new_value
        if isinstance(operation, SetRowOperation):
            row = data.get(operation.id)
            return (
                isinstance(row, dict)
                and row.get(operation.key) == operation.new_value
            )
        return False

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

    def _collect_policies(
        self, target: Node, removed_id: str
    ) -> list[Instruction]:
        """Expand CASCADE against the logical state at the deletion boundary.

        Triggers run before this boundary and caller instructions resume
        after it. Reparented children survive; old dependents are deleted
        before a caller reinserts the same parent identifier. Multiple paths
        and cycles schedule each existing row at most once per wave.
        """
        derived: dict[tuple[Node, str], Instruction] = {}
        for foreign_key in self.engine.metadata.referenced_by(
            target.schema.name
        ):
            if foreign_key.on_delete is not ForeignKeyPolicy.CASCADE:
                continue
            node = self.engine.get_node_by_schema(foreign_key.schema)
            for row_id, row in self.delta.data(node).items():
                if row.get(foreign_key.field) == removed_id:
                    derived[(node, row_id)] = RemoveInstruction(
                        address=node.address,
                        id=row_id,
                        source=OperationSource.POLICY,
                    )
        return list(derived.values())

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
