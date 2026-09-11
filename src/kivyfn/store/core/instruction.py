# src/kivyfn/store/core/instruction.py
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, TypeAlias

from kivyfn.store.address import Address
from kivyfn.store.error import InvalidInstructionError
from kivyfn.store.operations import OpCode, OperationSource


CommandPayload: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class SetInstruction:
    """Primitive single-schema field mutation."""

    opcode: ClassVar[OpCode] = OpCode.SET

    address: Address
    key: str
    value: Any
    source: OperationSource


@dataclass(frozen=True, slots=True, kw_only=True)
class SetRowInstruction:
    """Primitive many-schema row field mutation."""

    opcode: ClassVar[OpCode] = OpCode.SETROW

    address: Address
    id: str
    key: str
    value: Any
    source: OperationSource


@dataclass(frozen=True, slots=True, kw_only=True)
class InsertInstruction:
    """Primitive many-schema row insertion."""

    opcode: ClassVar[OpCode] = OpCode.INSERT

    address: Address
    id: str
    value: dict[str, Any]
    source: OperationSource


@dataclass(frozen=True, slots=True, kw_only=True)
class RemoveInstruction:
    """Primitive many-schema row removal."""

    opcode: ClassVar[OpCode] = OpCode.REMOVE

    address: Address
    id: str
    source: OperationSource


Instruction: TypeAlias = (
    SetInstruction
    | SetRowInstruction
    | InsertInstruction
    | RemoveInstruction
)


def instruction_from_payload(
    payload: CommandPayload,
    source: OperationSource,
) -> Instruction:
    opcode = _opcode(
        payload,
    )
    address = _address(
        payload,
    )

    match opcode:
        case OpCode.SET:
            return SetInstruction(
                address=address,
                key=_string(
                    payload,
                    "key",
                ),
                value=payload.get(
                    "value",
                ),
                source=source,
            )

        case OpCode.SETROW:
            return SetRowInstruction(
                address=address,
                id=_string(
                    payload,
                    "id",
                ),
                key=_string(
                    payload,
                    "key",
                ),
                value=payload.get(
                    "value",
                ),
                source=source,
            )

        case OpCode.INSERT:
            value = payload.get(
                "value",
            )

            if not isinstance(
                value,
                dict,
            ):
                raise InvalidInstructionError(
                    "INSERT requires a dictionary value"
                )

            return InsertInstruction(
                address=address,
                id=_string(
                    payload,
                    "id",
                ),
                value=dict(
                    value
                ),
                source=source,
            )

        case OpCode.REMOVE:
            return RemoveInstruction(
                address=address,
                id=_string(
                    payload,
                    "id",
                ),
                source=source,
            )


def instructions_from_payload(
    commands: Iterable[CommandPayload],
    source: OperationSource,
) -> list[Instruction]:
    return [
        instruction_from_payload(
            command,
            source,
        )
        for command in commands
    ]


def _opcode(
    payload: CommandPayload,
) -> OpCode:
    value = payload.get(
        "opcode",
    )

    try:
        return OpCode(
            value
        )
    except (TypeError, ValueError) as exc:
        raise InvalidInstructionError(
            f"Unsupported opcode: {value!r}"
        ) from exc


def _address(
    payload: CommandPayload,
) -> Address:
    value = payload.get(
        "address",
    )

    if not isinstance(
        value,
        (list, tuple),
    ):
        raise InvalidInstructionError(
            "Instruction address must be a list or tuple"
        )

    if not value:
        raise InvalidInstructionError(
            "Instruction address cannot be empty"
        )

    if not all(
        isinstance(part, str) and part
        for part in value
    ):
        raise InvalidInstructionError(
            "Instruction address parts must be non-empty strings"
        )

    return Address(
        *value,
    )


def _string(
    payload: CommandPayload,
    key: str,
) -> str:
    value = payload.get(
        key,
    )

    if not isinstance(
        value,
        str,
    ) or not value:
        raise InvalidInstructionError(
            f"Instruction {key} must be a non-empty string"
        )

    return value
