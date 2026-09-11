# src/kivyfn/store/store/instructions.py
from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

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
    InsertRequest,
    RemoveRequest,
    SetRequest,
    SetRowRequest,
)

if TYPE_CHECKING:
    from kivyfn.store.core.metadata import EngineMetadata


class InstructionBuilder:
    """Translate one ordered mutation group without changing Store data.

    Schema lookup belongs to translation. The compiler owns field validation,
    defaults, triggers, and schema-kind checks. Store owns synchronization and
    submits the complete instruction tuple to Engine as one transaction.
    """

    def __init__(self, metadata: EngineMetadata) -> None:
        self.metadata = metadata

    def build(
        self,
        requests: tuple[
            SetRequest | SetRowRequest | InsertRequest | RemoveRequest, ...
        ],
    ) -> tuple[Instruction, ...]:
        instructions: list[Instruction] = []

        for request in requests:
            try:
                address = self.metadata.nodes_by_schema[request.schema].address
            except KeyError:
                raise NodeNotFoundError(
                    f"No node found for schema {request.schema!r}",
                    detail=NodeErrorDetail(schema=request.schema),
                ) from None

            match request:
                case SetRequest():
                    instructions.extend(
                        SetInstruction(
                            address=address,
                            key=key,
                            value=value,
                            source=OperationSource.COMMAND,
                        )
                        for key, value in request.values
                    )
                case SetRowRequest():
                    instructions.extend(
                        SetRowInstruction(
                            address=address,
                            id=request.row_id,
                            key=key,
                            value=value,
                            source=OperationSource.COMMAND,
                        )
                        for key, value in request.values
                    )
                case InsertRequest():
                    instructions.append(
                        InsertInstruction(
                            address=address,
                            id=request.row_id,
                            value=dict(request.values),
                            source=OperationSource.COMMAND,
                        )
                    )
                case RemoveRequest():
                    instructions.append(
                        RemoveInstruction(
                            address=address,
                            id=request.row_id,
                            source=OperationSource.COMMAND,
                        )
                    )
                case _:
                    assert_never(request)

        return tuple(instructions)
