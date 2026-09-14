# src/kivyfn/store/store/instructions.py
from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias, assert_never

from kivyfn.store.address import Address
from kivyfn.store.core.instruction import (
    InsertInstruction,
    Instruction,
    RemoveInstruction,
    SetInstruction,
    SetRowInstruction,
)
from kivyfn.store.error import NodeErrorDetail, NodeNotFoundError
from kivyfn.store.operations import OperationSource
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    RemoveRequestAction,
    SetRequestAction,
    SetRowRequestAction,
)


if TYPE_CHECKING:
    from kivyfn.store.core.metadata import EngineMetadata


StoreRequestAction: TypeAlias = (
    SetRequestAction
    | SetRowRequestAction
    | InsertRequestAction
    | RemoveRequestAction
)


class InstructionBuilder:
    """Translate Store request actions into Engine instructions."""

    def __init__(self, metadata: EngineMetadata) -> None:
        self._metadata = metadata

    def build(
        self, actions: tuple[StoreRequestAction, ...]
    ) -> tuple[Instruction, ...]:
        instructions: list[Instruction] = []

        for action in actions:
            address = self._address(action.schema)

            match action:
                case SetRequestAction():
                    instructions.extend(
                        SetInstruction(
                            address=address,
                            key=key,
                            value=value,
                            source=OperationSource.COMMAND,
                        )
                        for key, value in action.values
                    )

                case SetRowRequestAction():
                    instructions.extend(
                        SetRowInstruction(
                            address=address,
                            id=action.row_id,
                            key=key,
                            value=value,
                            source=OperationSource.COMMAND,
                        )
                        for key, value in action.values
                    )

                case InsertRequestAction():
                    instructions.append(
                        InsertInstruction(
                            address=address,
                            id=action.row_id,
                            value=dict(action.values),
                            source=OperationSource.COMMAND,
                        )
                    )

                case RemoveRequestAction():
                    instructions.append(
                        RemoveInstruction(
                            address=address,
                            id=action.row_id,
                            source=OperationSource.COMMAND,
                        )
                    )

                case _:
                    assert_never(action)

        return tuple(instructions)

    def _address(self, schema: str) -> Address:
        try:
            return self._metadata.nodes_by_schema[schema].address

        except KeyError:
            raise NodeNotFoundError(
                f"No node found for schema {schema!r}",
                detail=NodeErrorDetail(schema=schema),
            ) from None
