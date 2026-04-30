from __future__ import annotations
from typing import Any
from .constants import ConstraintType


class StoreBaseError(Exception):
    pass


class EngineExecutionError(StoreBaseError):
    pass


class CompilerError(EngineExecutionError):
    def __init__(
        self,
        message: str,
        *,
        instruction=None,
        node=None,
        schema=None,
    ):
        super().__init__(message)
        self.message = message
        self.instruction = instruction
        self.node = node
        self.schema = schema

    def __repr__(self):
        base = super().__repr__()
        if self.instruction:
            return f"{base} (instruction={self.instruction})"
        return base


class FieldError(CompilerError):
    def __init__(
        self,
        field: str,
        message: str,
        *,
        value=None,
        metadata: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.field = field
        self.value = value
        self.metadata = metadata or {}


class FieldNotFoundError(CompilerError):
    pass


class ConstraintError(EngineExecutionError):
    """
    Raised when the proposed future state violates
    a relational invariant.
    """

    def __init__(
        self,
        message: str,
        *,
        constraint_type: ConstraintType,
        schema: Any | None = None,
        field: str | None = None,
        value: Any | None = None,
    ):
        super().__init__(message)

        self.constraint_type = constraint_type
        self.schema = schema
        self.field = field
        self.value = value

    def __repr__(self) -> str:
        schema_name = getattr(self.schema, "__name__", self.schema)

        return (
            f"{self.__class__.__name__}("
            f"type={self.constraint_type}, "
            f"schema={schema_name}, "
            f"field={self.field}, "
            f"value={self.value}"
            f")"
        )


class AddressError(EngineExecutionError):
    pass


class SchemaError(StoreBaseError):
    pass


class DocumentError(StoreBaseError):
    pass


class NodeError(StoreBaseError):
    pass
