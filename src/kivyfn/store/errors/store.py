from __future__ import annotations
from typing import Any, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from kivyfn.store.core.instruction import Instruction


class StoreError(Exception):
    code = "STORE_ERROR"
    status = 400

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def payload(self) -> dict[str, Any]:
        payload = {
            "type": self.__class__.__name__,
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }

        return payload


class StoreExecutionError(StoreError):
    code = "STORE_EXECUTION_ERROR"


class CompilerError(StoreExecutionError):
    code = "COMPILE_ERROR"

    def payload(self) -> dict[str, Any]:
        context = dict(self.context)

        node = context.get("node")
        if node:
            context["node"] = node.name

        schema = context.get("schema")
        if schema:
            context["schema"] = schema.name

        instruction: Instruction | None = context.get("instruction")
        if instruction:
            context["instruction"] = {
                "opcode": instruction.opcode.name,
                "address": instruction.address.to_list(),
                "id": instruction.id,
                "key": instruction.key,
            }

        payload = super().payload()
        payload["context"] = context
        return payload

class ConstraintError(StoreExecutionError):
    code = "CONSTRAINT_ERROR"

    def payload(self) -> dict[str, Any]:
        payload = super().payload()
        context = dict(payload["context"])

        schema = context.get("schema")
        if schema is not None:
            context["schema"] = getattr(schema, "name", None)

        constraint_type = context.get("constraint_type")
        if constraint_type is not None:
            context["constraint_type"] = constraint_type.name

        payload["context"] = context
        return payload

class AddressError(StoreExecutionError):
    code = "ADDRESS_ERROR"


class NodeNotFoundError(StoreExecutionError):
    code = "NODE_NOT_FOUND"


class FieldError(StoreExecutionError):
    code = "FIELD_ERROR"


class FieldNotFoundError(FieldError):
    code = "FIELD_NOT_FOUND"


class FieldValidationError(FieldError):
    code = "FIELD_VALIDATION_ERROR"



