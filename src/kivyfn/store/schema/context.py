# kivyfn/store/schema/context.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.schema.fields import ForeignKeyField
from kivyfn.store.core.operations import (
    InsertOperation,
    Operation,
    RemoveOperation,
    SetOperation,
    SetRowOperation,
)

if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.core.instruction import Instruction
    from kivyfn.store.node import Node


@dataclass(frozen=True, slots=True)
class ForeignKeyReference:
    """
    A resolved foreign-key reference.

    id:
        Referenced row identifier.

    node:
        Node containing the referenced row.

    row:
        Logical referenced row, including visible transaction changes.
    """

    id: str
    node: Node
    row: dict[str, Any]


class TriggerContext:
    """
    Read-only trigger execution context.

    The context combines:

    - committed Node state;
    - operations already compiled in the current transaction;
    - the operation that caused the current trigger.

    Operations in ``operations`` are expected to include
    ``operation`` itself.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        operation: Operation,
        operations: list[Operation],
    ) -> None:
        self.engine = engine
        self.operation = operation

        # Keep a stable, read-only snapshot of the visible operations.
        self.operations = tuple(operations)

        if operation not in self.operations:
            raise ValueError(
                "Current trigger operation must be present in operations"
            )

    # ============================================================
    # Current operation context
    # ============================================================

    @property
    def node(self) -> Node:
        return self.operation.node  # pyright: ignore[reportAttributeAccessIssue]

    @property
    def instruction(self) -> Instruction:
        return self.operation.instruction

    @property
    def opcode(self) -> InstructionOpCode:
        return self.operation.opcode

    @property
    def id(self) -> str | None:
        return self.instruction.id

    @property
    def key(self) -> str | None:
        return self.instruction.key

    @property
    def value(self) -> Any:
        return self.instruction.value

    # ============================================================
    # Current value resolution
    # ============================================================

    def current(self) -> dict[str, Any]:
        """
        Resolve the logical value relevant to the current trigger.

        SET:
            Single-schema value after the SET operation.

        INSERT:
            Inserted row after the INSERT operation.

        SETROW:
            Many-schema row after the SETROW operation.

        REMOVE:
            Many-schema row immediately before the REMOVE operation.

        REMOVE uses the before-state because the row no longer exists after
        the operation.
        """

        if self.opcode is InstructionOpCode.SET:
            return self._current_set()

        if self.opcode is InstructionOpCode.INSERT:
            return self._current_insert()

        if self.opcode is InstructionOpCode.SETROW:
            return self._current_setrow()

        if self.opcode is InstructionOpCode.REMOVE:
            return self._current_remove()

        raise RuntimeError(
            f"Unsupported trigger opcode: {self.opcode!r}"
        )

    def _current_set(self) -> dict[str, Any]:
        self._require_single(self.node)

        return self.single(
            self.node,
            through=self.operation,
        )

    def _current_insert(self) -> dict[str, Any]:
        self._require_many(self.node)

        row_id = self._require_id()

        row = self.row(
            self.node,
            row_id,
            through=self.operation,
        )

        if row is None:
            raise RuntimeError(
                f"Could not resolve inserted row "
                f"{self.node.schema.name}[{row_id!r}]"
            )

        return row

    def _current_setrow(self) -> dict[str, Any]:
        self._require_many(self.node)

        row_id = self._require_id()

        row = self.row(
            self.node,
            row_id,
            through=self.operation,
        )

        if row is None:
            raise RuntimeError(
                f"Could not resolve updated row "
                f"{self.node.schema.name}[{row_id!r}]"
            )

        return row

    def _current_remove(self) -> dict[str, Any]:
        self._require_many(self.node)

        row_id = self._require_id()

        row = self.row(
            self.node,
            row_id,
            before=self.operation,
        )

        if row is None:
            raise RuntimeError(
                f"Could not resolve removed row "
                f"{self.node.schema.name}[{row_id!r}]"
            )

        return row

    # ============================================================
    # Many-schema resolution
    # ============================================================

    def row(
        self,
        node: Node,
        row_id: str,
        *,
        before: Operation | None = None,
        through: Operation | None = None,
    ) -> dict[str, Any] | None:
        """
        Resolve a logical row from committed state plus visible operations.

        With no boundary:
            Applies every visible operation.

        before=operation:
            Applies operations before the boundary operation, excluding it.

        through=operation:
            Applies operations through the boundary operation, including it.

        ``before`` and ``through`` are mutually exclusive.
        """

        self._require_many(node)
        self._validate_boundary(before, through)

        committed = node.snapshot(id=row_id)

        if isinstance(committed, dict):
            resolved: dict[str, Any] | None = dict(committed)
        else:
            resolved = None

        boundary = before if before is not None else through

        for operation in self.operations:
            if before is not None and operation is boundary:
                break

            resolved = self._apply_row_operation(
                operation=operation,
                target_node=node,
                target_id=row_id,
                resolved=resolved,
            )

            if through is not None and operation is boundary:
                break

        if boundary is not None and boundary not in self.operations:
            raise ValueError(
                "Resolution boundary is not visible in this trigger context"
            )

        if resolved is None:
            return None

        return dict(resolved)

    def _apply_row_operation(
        self,
        *,
        operation: Operation,
        target_node: Node,
        target_id: str,
        resolved: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        operation_node = operation.node  # pyright: ignore[reportAttributeAccessIssue]

        if operation_node is not target_node:
            return resolved

        if isinstance(operation, InsertOperation):
            if operation.id != target_id:
                return resolved

            return dict(operation.value)

        if isinstance(operation, SetRowOperation):
            if operation.id != target_id:
                return resolved

            if resolved is None:
                return None

            updated = dict(resolved)
            updated[operation.key] = operation.new_value
            return updated

        if isinstance(operation, RemoveOperation):
            if operation.id != target_id:
                return resolved

            return None

        return resolved

    # ============================================================
    # Single-schema resolution
    # ============================================================

    def single(
        self,
        node: Node,
        *,
        before: Operation | None = None,
        through: Operation | None = None,
    ) -> dict[str, Any]:
        """
        Resolve a logical single-schema value.

        Boundary behavior matches ``row()``.
        """

        self._require_single(node)
        self._validate_boundary(before, through)

        committed = node.snapshot()

        if isinstance(committed, dict):
            resolved = dict(committed)
        else:
            resolved = {}

        boundary = before if before is not None else through

        for operation in self.operations:
            if before is not None and operation is boundary:
                break

            operation_node = (
                operation.node  # pyright: ignore[reportAttributeAccessIssue]
            )

            if operation_node is node and isinstance(operation, SetOperation):
                resolved[operation.key] = operation.new_value

            if through is not None and operation is boundary:
                break

        if boundary is not None and boundary not in self.operations:
            raise ValueError(
                "Resolution boundary is not visible in this trigger context"
            )

        return resolved

    # ============================================================
    # Foreign-key resolution
    # ============================================================
    def resolve_foreignkey(
        self,
        field_name: str,
    ) -> ForeignKeyReference | None:
        """
        Resolve a foreign key from the current logical value.

        Only the field name is required because the context already knows:

        - the source node;
        - the source schema;
        - the current operation;
        - the logical source row/value;
        - the store engine.

        The referenced row is resolved from committed state plus all
        operations visible to this trigger.
        """
        field = self.node.schema.get_field(
            field_name,
        )

        if not isinstance(field, ForeignKeyField):
            raise TypeError(
                f"{self.node.schema.name}.{field_name} "
                "is not a ForeignKeyField"
            )

        source = self.current()
        foreign_id = source.get(
            field_name,
        )

        if foreign_id is None:
            return None

        if not isinstance(foreign_id, str):
            raise TypeError(
                f"{self.node.schema.name}.{field_name} must contain "
                f"a string row id, got {type(foreign_id).__name__}"
            )

        target_node = self.engine.get_node_by_schema(
            field.schema,
        )

        target_row = self.row(
            target_node,
            foreign_id,
        )

        if target_row is None:
            raise RuntimeError(
                "Could not resolve foreign-key target "
                f"{self.node.schema.name}.{field_name}="
                f"{foreign_id!r} referencing "
                f"{target_node.schema.name}"
            )

        return ForeignKeyReference(
            id=foreign_id,
            node=target_node,
            row=target_row,
        )

    def require_foreignkey(
        self,
        field_name: str,
    ) -> ForeignKeyReference:
        reference = self.resolve_foreignkey(
            field_name,
        )

        if reference is None:
            raise RuntimeError(
                f"{self.node.schema.name}.{field_name} "
                "does not contain a foreign-key value"
            )

        return reference


    # ============================================================
    # Helpers
    # ============================================================

    def _require_id(self) -> str:
        row_id = self.instruction.id

        if row_id is None:
            raise RuntimeError(
                f"{self.opcode.value} operation requires a row id"
            )

        return row_id

    def _require_many(self, node: Node) -> None:
        if not node.schema.is_many():
            raise TypeError(
                f"{node.schema.name} is a single schema; "
                "row operations require a many schema"
            )

    def _require_single(self, node: Node) -> None:
        if node.schema.is_many():
            raise TypeError(
                f"{node.schema.name} is a many schema; "
                "single-value operations require a single schema"
            )

    def _validate_boundary(
        self,
        before: Operation | None,
        through: Operation | None,
    ) -> None:
        if before is not None and through is not None:
            raise ValueError(
                "Use either 'before' or 'through', not both"
            )
