from __future__ import annotations

from typing import Any, TYPE_CHECKING


from kivyfn.store.constants import InstructionOpCode
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store import Store
    from kivyfn.store.address import Address
    from kivyfn.store.transport.response import Response
    from kivyfn.store.client import StoreClient


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
        self,
        store: Store,
        commands: list[CommandPayload] | None = None,
    ):
        super().__init__(commands)
        self._store = store

    async def commit(self) -> Response[TransactionResult]:
        await self._store.ready()
        return await self._store.client.transaction(self.commands)


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



