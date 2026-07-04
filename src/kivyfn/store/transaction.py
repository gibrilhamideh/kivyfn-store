# kivyfn/store/transaction.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from kivyfn.store import Store
    from kivyfn.store.address import Address
    from kivyfn.store.transport.response import Response


CommandPayload = dict[str, Any]


class TransactionBuilder:
    def __init__(
        self,
        commands: list[CommandPayload] | None = None,
    ) -> None:
        self._commands: list[CommandPayload] = (
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
                "opcode": InstructionOpCode.SET,
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
                "opcode": InstructionOpCode.SETROW,
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
                "opcode": InstructionOpCode.INSERT,
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
        self,
        store: Store,
        commands: list[CommandPayload] | None = None,
    ) -> None:
        super().__init__(commands)
        self._store = store

    async def commit(
        self,
    ) -> Response[TransactionResult]:
        await self._store.ready()

        return await self._store.client.transaction(
            self.commands
        )


class TransactionResult:
    def __init__(
        self,
        *,
        success: bool,
        stage: str,
        compiled: list[Any] | None = None,
        error: Exception | None = None,
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
            lines.append("  Compiled:")

            for operation in self.compiled:
                lines.append(f"    - {operation}")

        if self.error is not None:
            lines.append("  Error:")
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
                lines.append("  Failed Action:")
                lines.append(
                    f"    intent={action.intent.name}, "
                    f"address={action.address}, "
                    f"id={action.id}, "
                    f"key={action.key}, "
                    f"source={action.source.name}"
                )

            cause = self.error.__cause__

            if cause is not None:
                lines.append("  Root Cause:")
                lines.append(
                    f"    {type(cause).__name__}: "
                    f"{cause}"
                )

        return "\n".join(lines)

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> TransactionResult:
        return cls(
            success=payload["success"],
            stage=payload["stage"],
            compiled=payload.get("compiled"),
            inserted=payload.get("inserted"),
            removed=payload.get("removed"),
            updated=payload.get("updated"),
            error=payload.get("error"),
        )



