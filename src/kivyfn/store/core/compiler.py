from __future__ import annotations
from typing import TYPE_CHECKING
from collections import deque

from ..node import Node
from ..errors import CompilerError, FieldError
from ..constants import Policy, InstructionOpCode, InstructionSource

from .instruction import Instruction
from .registry import ForeignKeyRegistry
from .operations import (
    Operation,
    InsertOperation,
    RemoveOperation,
    SetOperation,
    SetRowOperation,
)


if TYPE_CHECKING:
    from .engine import Engine


class Compiler:
    def __init__(self, engine: Engine, instructions: list[Instruction]) -> None:
        self.engine = engine
        self.pending = deque(instructions)
        self.operations: list[Operation] = []

    def compile(self) -> list[Operation]:
        while self.pending:
            instruction = self.pending.popleft()

            node = self.engine.resolve(instruction.address)

            # 1️⃣ compile base mutation
            operation, instruction = self._compile_action(node, instruction)
            self.operations.append(operation)

            # 2️⃣ collect derived actions
            derived = []
            derived.extend(self._collect_triggers(node, instruction))
            derived.extend(self._collect_policies(node, instruction))

            # 3️⃣ push back into queue
            for new_action in derived:
                self.pending.append(new_action)

        return self.operations

    # ============================= #
    # Action Compiler
    # ============================= #
    def _compile_action(
        self, node: Node, instruction: Instruction
    ) -> tuple[Operation, Instruction]:
        if instruction.opcode is InstructionOpCode.SET:
            return self._compile_set(node, instruction)

        if instruction.opcode is InstructionOpCode.SETROW:
            return self._compile_setrow(node, instruction)

        if instruction.opcode is InstructionOpCode.INSERT:
            return self._compile_insert(node, instruction)

        if instruction.opcode is InstructionOpCode.REMOVE:
            return self._compile_remove(node, instruction)

        raise RuntimeError(f"Unknown action intent: {instruction.opcode}")

    # ============================= #
    # Operation Compliler
    # ============================= #
    def _compile_set(self, node: Node, instruction: Instruction):
        schema = node.schema

        if schema.is_many():
            raise CompilerError(
                "SET only valid on single schema",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        if instruction.key is None:
            raise CompilerError(
                "SET requires key",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        try:
            schema.challenge_field(instruction.key, instruction.value)
        except FieldError as e:
            raise CompilerError(
                e.message,
                node=node,
                schema=schema,
                instruction=instruction,
            ) from e

        return SetOperation(node, instruction), instruction

    def _compile_setrow(self, node: Node, instruction: Instruction):
        schema = node.schema

        if not schema.is_many():
            raise CompilerError(
                "SETROW only valid on many schema",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        if instruction.id is None:
            raise CompilerError(
                "SETROW requires id",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        if instruction.key is None:
            raise CompilerError(
                "SETROW requires key",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        try:
            schema.challenge_field(instruction.key, instruction.value)
        except FieldError as e:
            raise CompilerError(
                e.message,
                node=node,
                schema=schema,
                instruction=instruction,
            ) from e

        return SetRowOperation(node, instruction), instruction

    def _compile_insert(self, node: Node, instruction: Instruction):
        schema = node.schema

        if not schema.is_many():
            raise CompilerError(
                "INSERT only valid on many schema",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        if instruction.id is None:
            raise CompilerError(
                "INSERT requires id",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        if not isinstance(instruction.value, dict):
            raise CompilerError(
                "INSERT requires dict value",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        try:
            built = node._build_default(instruction.value)
        except FieldError as e:
            raise CompilerError(
                e.message,
                node=node,
                schema=schema,
                instruction=instruction,
            ) from e

        instruction = instruction.copy(value=built)

        return InsertOperation(node, instruction), instruction

    def _compile_remove(
        self, node: Node, instruction: Instruction
    ) -> tuple[Operation, Instruction]:
        schema = node.schema

        if not schema.is_many():
            raise CompilerError(
                "REMOVE only valid on many schema",
                node=node,
                schema=schema,
                instruction=instruction,
            )

        if instruction.id is None:
            raise CompilerError(
                "REMOVE requires id", node=node, schema=schema, instruction=instruction
            )

        return RemoveOperation(node, instruction), instruction

    # ============================= #
    # Trigger Executions
    # ============================= #
    def _collect_triggers(
        self, node: Node, instruction: Instruction
    ) -> list[Instruction]:
        derived: list[Instruction] = []

        triggers = getattr(node.schema, "__triggers__", [])
        # NOTE: must solve hiddne import
        from .transaction import Transaction

        for trigger in triggers:
            if not trigger.matches(instruction.opcode, instruction.key):
                continue

            # internal execution context to collect new actions
            exec_ctx = Transaction(
                self.engine, _internal=True, _source=InstructionSource.TRIGGER
            )

            # call trigger
            trigger.fn(node, instruction, exec_ctx)

            # collect generated actions
            derived.extend(exec_ctx._instructions)

        return derived

    # ============================= #
    # Policies Executions
    # ============================= #
    def _collect_policies(
        self, node: Node, instruction: Instruction
    ) -> list[Instruction]:
        # Only REMOVE triggers FK policies
        if instruction.opcode is not InstructionOpCode.REMOVE:
            return []

        derived: list[Instruction] = []

        target_schema = node.schema.__name__
        removed_id = instruction.id

        # Get all foreign keys referencing this schema
        foreign_keys = ForeignKeyRegistry.referenced_by(target_schema)

        if not foreign_keys:
            return []

        for fk in foreign_keys:
            # Since one schema = one node
            node = self.engine.get_node_by_schema(fk.schema)

            # Find rows referencing removed_id
            ids = node.lookup(fk.field, removed_id)

            if not ids:
                continue

            # Apply policy
            if fk.on_delete is Policy.RESTRICT:
                raise CompilerError(
                    f"Cannot delete {target_schema} '{removed_id}' "
                    f"because it is referenced by "
                    f"{fk.schema}.{fk.field}",
                    node=node,
                    schema=node.schema,
                    instruction=instruction,
                )

            if fk.on_delete is Policy.CASCADE:
                for id in ids:
                    derived.append(
                        Instruction.remove(
                            node.address(),  # lazy computed address
                            id,
                            source=InstructionSource.POLICY,
                        )
                    )

        return derived
