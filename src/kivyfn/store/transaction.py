# kivyfn/store/transaction.py
from __future__ import annotations

from typing import Any, Self, TypeAlias

from kivyfn.store.address import Address
from kivyfn.store.operations import OpCode
from kivyfn.store.utils.tools.uuid import generate_uuid


CommandPayload: TypeAlias = dict[str, Any]


class Transaction:
    def __init__(
        self,
        commands: list[CommandPayload] | None = None,
    ) -> None:
        self._commands = (
            list(commands)
            if commands is not None
            else []
        )

    def set(
        self,
        address: Address[Any],
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
        address: Address[Any],
        id: str | None,
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
        address: Address[Any],
        value: dict[str, Any],
        id: str | None = None,
    ) -> Self:
        if id is None:
            id = generate_uuid()

        self._commands.append(
            {
                "opcode": OpCode.INSERT,
                "address": address.to_list(),
                "id": id,
                "value": value,
            }
        )

        return self

    def remove(
        self,
        address: Address[Any],
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

    def merge(
        self,
        address: Address[Any],
        id: str | None,
        value: dict[str, Any],
    ) -> Self:
        for key, item in value.items():
            self.setrow(
                address=address,
                id=id,
                key=key,
                value=item,
            )

        return self

    def extend(
        self,
        commands: list[CommandPayload],
    ) -> Self:
        self._commands.extend(
            commands,
        )

        return self

    def clear(self) -> Self:
        self._commands.clear()
        return self

    @property
    def commands(self) -> list[CommandPayload]:
        return list(
            self._commands,
        )

    def __len__(self) -> int:
        return len(
            self._commands,
        )

    def __bool__(self) -> bool:
        return bool(
            self._commands,
        )


TransactionInput: TypeAlias = (
    Transaction
    | list[CommandPayload]
)


def resolve_commands(
    transaction: TransactionInput,
) -> list[CommandPayload]:
    if isinstance(
        transaction,
        Transaction,
    ):
        return transaction.commands

    return list(
        transaction,
    )


class TransactionResult:
    def __init__(
        self,
        *,
        success: bool,
        stage: str,
        compiled: list[Any] | None = None,
        error: Exception | str | None = None,
        inserted: list[Any] | None = None,
        removed: list[Any] | None = None,
        updated: list[Any] | None = None,
    ) -> None:
        self.success = success
        self.stage = stage
        self.compiled = compiled or []
        self.error = error
        self.inserted = inserted or []
        self.removed = removed or []
        self.updated = updated or []

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> TransactionResult:
        return cls(
            success=payload["success"],
            stage=payload["stage"],
            compiled=payload.get(
                "compiled",
            ),
            inserted=payload.get(
                "inserted",
            ),
            removed=payload.get(
                "removed",
            ),
            updated=payload.get(
                "updated",
            ),
            error=payload.get(
                "error",
            ),
        )

    def __repr__(self) -> str:
        lines = [
            (
                "TransactionResult("
                f"success={self.success}, "
                f"stage={self.stage!r}"
                ")"
            )
        ]

        if self.compiled:
            lines.append(
                "  Compiled:"
            )

            for operation in self.compiled:
                lines.append(
                    f"    - {operation}"
                )

        if self.error is not None:
            lines.append(
                "  Error:"
            )

            if isinstance(
                self.error,
                Exception,
            ):
                lines.append(
                    "    "
                    f"{type(self.error).__name__}: "
                    f"{self.error}"
                )

                action = getattr(
                    self.error,
                    "action",
                    None,
                )

                if action is not None:
                    lines.append(
                        "  Failed Action:"
                    )

                    lines.append(
                        f"    intent={action.intent.name}, "
                        f"address={action.address}, "
                        f"id={action.id}, "
                        f"key={action.key}, "
                        f"source={action.source.name}"
                    )

                cause = self.error.__cause__

                if cause is not None:
                    lines.append(
                        "  Root Cause:"
                    )

                    lines.append(
                        f"    {type(cause).__name__}: "
                        f"{cause}"
                    )

            else:
                lines.append(
                    f"    {self.error}"
                )

        return "\n".join(
            lines,
        )



