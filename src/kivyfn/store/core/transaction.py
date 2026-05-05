from __future__ import annotations

from typing import Any, TYPE_CHECKING

from kivyfn.store.core.compiler import Compiler
from kivyfn.store.core.delta import Delta
from kivyfn.store.core.constraintguard import ConstraintGuard
from kivyfn.store.core.instruction import Instruction
from kivyfn.store.core.operations import (
    SetOperation,
    SetRowOperation,
    InsertOperation,
    RemoveOperation,
)


from kivyfn.store.constants import InstructionSource, InstructionOpCode
from kivyfn.store.errors import ConstraintError, CompilerError
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.events.event import NodeEvent
    from kivyfn.store.address import Address
    from kivyfn.store.transport.response import Response
    from kivyfn.store.store.client import StoreClient


CommandPayload = dict[str, Any]


class TransactionBuilder:
    def __init__(self, commands: list[CommandPayload] | None = None):
        self._commands: list[CommandPayload] = list(commands) if commands else []

    def set(self, address: Address, key: str, value: Any):
        self._commands.append(
            {
                "opcode": InstructionOpCode.SET,
                "address": address.to_list(),
                "key": key,
                "value": value,
            }
        )
        return self

    def setrow(self, address: Address, id: str | None, key: str, value: Any):
        self._commands.append(
            {
                "opcode": InstructionOpCode.SETROW,
                "address": address.to_list(),
                "id": id,
                "key": key,
                "value": value,
            }
        )
        return self

    def insert(self, address: Address, value: dict[str, Any], id: str | None = None):
        if id is None:
            id = generate_uuid()

        self._commands.append(
            {
                "opcode": InstructionOpCode.INSERT,
                "address": address.to_list(),
                "id": id,
                "value": value,
            }
        )
        return self

    def remove(self, address: Address, id: str):
        self._commands.append(
            {
                "opcode": InstructionOpCode.REMOVE,
                "address": address.to_list(),
                "id": id,
            }
        )
        return self

    @property
    def commands(self) -> list[CommandPayload]:
        return list(self._commands)


class Transaction(TransactionBuilder):
    def __init__(
        self, client: StoreClient, commands: list[CommandPayload] | None = None
    ):
        super().__init__(commands)
        self._client = client

    async def commit(self) -> Response[TransactionResult]:
        return await self._client.transaction(self.commands)


class EngineTransaction(TransactionBuilder):
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
        *args,
        _source: InstructionSource = InstructionSource.COMMAND,
    ):
        super().__init__(*args)
        self._source = _source
        self.engine = engine
        self.instructions = []

    # ----------------------------
    # Execute (only allowed for public Execution)
    # ----------------------------
    def execute(self) -> TransactionResult:
        self.instructions = Instruction.construct(self.commands, self._source)

        # --------------------------
        # Compile
        # --------------------------
        compiler = Compiler(self.engine, self.instructions)
        try:
            compiled_ops = compiler.compile()
        except CompilerError as e:
            return TransactionResult(
                success=False,
                stage="compile",
                compiled=compiler.operations,
                error=e,
            )

        # --------------------------
        # Build Delta
        # --------------------------
        try:
            delta = Delta(self.engine, compiled_ops)
        except CompilerError as e:
            return TransactionResult(
                success=False,
                stage="compile",
                compiled=compiled_ops,
                error=e,
            )

        # --------------------------
        # Validate
        # --------------------------

        try:
            ConstraintGuard(self.engine).validate(delta)
        except ConstraintError as e:
            return TransactionResult(
                success=False,
                stage="validate",
                compiled=compiled_ops,
                error=e,
            )

        # --------------------------
        # Execute (no try block)
        # --------------------------
        events: list[NodeEvent] = []
        executed_ops = []
        inserted = []
        removed = []
        updated = []

        for op in compiled_ops:
            event = op.execute()
            executed_ops.append(op)

            # Collect structured effects
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

        # --------------------------
        # Write to disk
        # --------------------------
        self.engine.to_disk(delta.touched_nodes)

        # --------------------------
        # Dispatch
        # --------------------------
        for event in events:
            self.engine.dispatch(event)

        # --------------------------
        # Result
        # --------------------------
        result = TransactionResult(
            success=True,
            stage="execute",
            compiled=compiled_ops,
            inserted=inserted,
            removed=removed,
            updated=updated,
        )

        return result


class TransactionResult:
    def __init__(
        self,
        *,
        success: bool,
        stage: str,
        compiled: list | None = None,
        error: Exception | None = None,
        inserted: list | None = None,
        removed: list | None = None,
        updated: list | None = None,
    ):
        self.success = success
        self.stage = stage
        self.compiled = compiled or []
        self.error = error

        self.inserted = inserted or []
        self.removed = removed or []
        self.updated = updated or []

    # -------------------------------------------------
    # Debug representation
    # -------------------------------------------------
    def __repr__(self) -> str:
        lines = [f"ExecutionResult(success={self.success}, stage='{self.stage}')"]

        if self.compiled:
            lines.append("  Compiled:")
            for op in self.compiled:
                lines.append(f"    - {op}")

        if self.error:
            lines.append("  Error:")
            lines.append(f"    {self.error.__class__.__name__}: {self.error}")

            # Show action cleanly if available
            action = getattr(self.error, "action", None)
            if action:
                lines.append("  Failed Action:")
                lines.append(
                    f"    intent={action.intent.name}, "
                    f"address={action.address}, "
                    f"id={action.id}, "
                    f"key={action.key}, "
                    f"source={action.source.name}"
                )

            # Optional: show original cause (FieldError)
            cause = getattr(self.error, "__cause__", None)
            if cause:
                lines.append("  Root Cause:")
                lines.append(f"    {cause.__class__.__name__}: {cause}")

        return "\n".join(lines)

    def payload(self) -> dict[str, Any]:
        data = {
            "success": self.success,
            "stage": self.stage,
            "inserted": self.inserted,
            "removed": self.removed,
            "updated": self.updated,
        }

        if self.error is not None:
            data["error"] = {
                "type": self.error.__class__.__name__,
                "message": str(self.error),
            }

        return data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TransactionResult":
        return cls(
            success=payload["success"],
            stage=payload["stage"],
            inserted=payload.get("inserted"),
            removed=payload.get("removed"),
            updated=payload.get("updated"),
            error=payload.get("error"),
        )
