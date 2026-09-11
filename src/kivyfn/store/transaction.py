
# src/kivyfn/store/transaction.py
from __future__ import annotations

from typing import Any, Self, TypeAlias

from kivyfn.store.address import Address
from kivyfn.store.operations import OpCode
from kivyfn.store.utils.tools.uuid import generate_uuid


CommandPayload: TypeAlias = dict[str, Any]


class Transaction:
    """Mutable builder for one atomic Store transaction."""

    def __init__(self) -> None:
        self._commands: list[CommandPayload] = []

    def set(
        self,
        address: Address,
        key: str,
        value: Any,
    ) -> Self:
        self._commands.append(
            {
                "opcode": OpCode.SET,
                "address": address.to_list(),
                "key": key,
                "value": value,
            }
        )

        return self

    def setrow(
        self,
        address: Address,
        id: str,
        key: str,
        value: Any,
    ) -> Self:
        self._commands.append(
            {
                "opcode": OpCode.SETROW,
                "address": address.to_list(),
                "id": id,
                "key": key,
                "value": value,
            }
        )

        return self

    def insert(
        self,
        address: Address,
        value: dict[str, Any],
        id: str | None = None,
    ) -> Self:
        row_id = (
            id
            if id is not None
            else generate_uuid()
        )

        self._commands.append(
            {
                "opcode": OpCode.INSERT,
                "address": address.to_list(),
                "id": row_id,
                "value": value,
            }
        )

        return self

    def remove(
        self,
        address: Address,
        id: str,
    ) -> Self:
        self._commands.append(
            {
                "opcode": OpCode.REMOVE,
                "address": address.to_list(),
                "id": id,
            }
        )

        return self

    @property
    def commands(self) -> tuple[CommandPayload, ...]:
        return tuple(
            self._commands
        )

    def payload(self) -> list[CommandPayload]:
        return [
            dict(command)
            for command in self._commands
        ]

    def __len__(self) -> int:
        return len(
            self._commands
        )

    def __bool__(self) -> bool:
        return bool(
            self._commands
        )




