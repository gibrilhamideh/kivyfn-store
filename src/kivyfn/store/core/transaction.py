from __future__ import annotations
from typing import TYPE_CHECKING

from .compiler import Compiler
from .delta import Delta
from .constraintguard import ConstraintGuard
from .instruction import Instruction
from .operations import SetOperation, SetRowOperation, InsertOperation, RemoveOperation

from ..constants import InstructionSource
from ..errors import EngineExecutionError, ConstraintError, CompilerError
from ..utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from .engine import Engine
    from ..events.event import NodeEvent


class TransactionResult:
    def __init__(
        self,
        *,
        engine,
        success: bool,
        stage: str,
        compiled: list | None = None,
        error: Exception | None = None,
        events: list[NodeEvent] | None = None,
        inserted: list | None = None,
        removed: list | None = None,
        updated: list | None = None,
    ):
        self.__engine = engine

        self.success = success
        self.stage = stage
        self.compiled = compiled or []
        self.error = error
        self.events = events or []

        self.inserted = inserted or []
        self.removed = removed or []
        self.updated = updated or []

    def dispatch(self, event: NodeEvent):
        self.__engine.dispatch(event)

    def dispatch_all(self):
        if not self.events:
            return

        for event in self.events:
            self.__engine.dispatch(event)

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


class Transaction:
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
        _internal: bool = False,
        _source: InstructionSource = InstructionSource.COMMAND,
    ):
        self.engine = engine
        self._instructions: list[Instruction] = []
        self._internal = _internal
        self._source = _source

    # ============================================================
    # Single-schema (ObjectNode) Commands
    # ============================================================

    def set(self, address, key, value):
        """
        Set a single field on a single-schema node.

        Equivalent to:
            node.value[key] = value

        Parameters:
            address : Node address resolving to a single schema
            key     : Field name
            value   : New value for the field

        Raises:
            OperationError if used on a many-schema node.
        """
        self._instructions.append(
            Instruction.set(address, key, value, source=self._source)
        )
        return self

    def merge(self, address, data: dict):
        """
        Set multiple fields on a single-schema node.

        This is a convenience wrapper around multiple `set()` calls.

        Equivalent to:
            for key, value in data.items():
                node.value[key] = value

        Parameters:
            address : Node address resolving to a single schema
            data    : Dictionary of field-value pairs

        Raises:
            OperationError if used on a many-schema node.
        """
        for key, value in data.items():
            self.set(address, key, value)
        return self

    # ============================================================
    # Many-schema (CollectionNode) Commands
    # ============================================================

    def setrow(self, address, id, key, value):
        """
        Set a single field inside a row of a collection node.

        Equivalent to:
            node.value[id][key] = value

        Parameters:
            address : Node address resolving to a many schema
            id      : Row identifier
            key     : Field name inside the row
            value   : New value for the field

        Raises:
            OperationError if used on a single-schema node.
        """
        self._instructions.append(
            Instruction.setrow(address, id, key, value, source=self._source)
        )
        return self

    def mergerow(self, address, id, data: dict):
        """
        Set multiple fields inside a row of a collection node.

        This is a convenience wrapper around multiple `setrow()` calls.

        Equivalent to:
            for key, value in data.items():
                node.value[id][key] = value

        Parameters:
            address : Node address resolving to a many schema
            id      : Row identifier
            data    : Dictionary of field-value pairs

        Raises:
            OperationError if used on a single-schema node.
        """
        for key, value in data.items():
            self.setrow(address, id, key, value)
        return self

    def insert(self, address, value: dict, id: str | None = None):
        """
        Insert a new row into a collection node.

        Equivalent to:
            node.value[id] = value

        Parameters:
            address : Node address resolving to a many schema
            value   : Dictionary representing row fields
            id      : Optional row identifier. If not provided,
                    a UUID will be generated.

        Returns:
            The row id used for insertion.

        Notes:
            ID generation happens at Execution levelt
            never during compilation.
        """
        if id is None:
            id = generate_uuid()

        self._instructions.append(
            Instruction.insert(address, id, value, source=self._source)
        )
        return self

    def remove(self, address, id):
        """
        Remove a row from a collection node.

        Equivalent to:
            del node.value[id]

        Parameters:
            address : Node address resolving to a many schema
            id      : Row identifier

        Raises:
            OperationError if used on a single-schema node.
        """
        self._instructions.append(Instruction.remove(address, id, source=self._source))
        return self

    # ----------------------------
    # Execute (only allowed for public Execution)
    # ----------------------------
    def commit(self, dispatch=True) -> TransactionResult:
        if self._internal:
            raise EngineExecutionError("Internal execution cannot be executed directly")

        compiler = Compiler(self.engine, self._instructions)

        # --------------------------
        # Compile
        # --------------------------
        try:
            compiled_ops = compiler.compile()
        except CompilerError as e:
            return TransactionResult(
                engine=self.engine,
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
                engine=self.engine,
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
                engine=self.engine,
                success=False,
                stage="validate",
                compiled=compiled_ops,
                error=e,
            )

        # --------------------------
        # Execute (no try block)
        # --------------------------
        events = []
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
                        "address": op.address,
                        "id": op.id,
                        "source": op.source,
                    }
                )

            elif isinstance(op, RemoveOperation):
                removed.append(
                    {
                        "address": op.address,
                        "id": op.id,
                        "source": op.source,
                    }
                )

            elif isinstance(op, (SetOperation, SetRowOperation)):
                updated.append(
                    {
                        "address": op.address,
                        "source": op.source,
                    }
                )

            if event is not None:
                events.append(event)

        # --------------------------
        # Write to disk
        # --------------------------
        self.engine.to_disk(delta.touched_nodes)

        # --------------------------
        # Result
        # --------------------------
        result = TransactionResult(
            engine=self.engine,
            success=True,
            stage="execute",
            compiled=compiled_ops,  # debug only
            events=events,
            inserted=inserted,
            removed=removed,
            updated=updated,
        )

        if dispatch:
            result.dispatch_all()

        return result
