# src/kivyfn/store/store/instructions.py
from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias, assert_never

from kivyfn.store.core.instruction import (
    InsertInstruction,
    Instruction,
    RemoveInstruction,
    SetInstruction,
    SetRowInstruction,
)
from kivyfn.store.error import NodeErrorDetail, NodeNotFoundError
from kivyfn.store.events.event import SignalEvent, SignalRowEvent, StoreSignal
from kivyfn.store.operations import OperationSource
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    RemoveRequestAction,
    SetRequestAction,
    SetRowRequestAction,
    SignalRequestAction,
    SignalRowRequestAction,
)


if TYPE_CHECKING:
    from kivyfn.store.core.metadata import EngineMetadata
    from kivyfn.store.node import Node


StoreRequestAction: TypeAlias = (
    SetRequestAction
    | SetRowRequestAction
    | InsertRequestAction
    | RemoveRequestAction
    | SignalRequestAction
    | SignalRowRequestAction
)


class InstructionBuilder:
    """Resolve Store request actions without changing Store data."""

    def __init__(self, metadata: EngineMetadata) -> None:
        self._metadata = metadata

    def build(
        self, actions: tuple[StoreRequestAction, ...]
    ) -> tuple[tuple[Instruction, ...], tuple[StoreSignal, ...]]:
        instructions: list[Instruction] = []
        signals: list[StoreSignal] = []

        for action in actions:
            node = self._node(action.schema)
            address = node.address

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

                case SignalRequestAction():
                    node.schema.get_signal(action.signal)

                    signals.append(
                        SignalEvent(
                            schema=node.schema.name,
                            source=OperationSource.COMMAND,
                            signal=action.signal,
                            data=action.data,
                        )
                    )

                case SignalRowRequestAction():
                    node.schema.get_signal(action.signal)

                    signals.append(
                        SignalRowEvent(
                            schema=node.schema.name,
                            source=OperationSource.COMMAND,
                            id=action.row_id,
                            signal=action.signal,
                            data=action.data,
                        )
                    )

                case _:
                    assert_never(action)

        return tuple(instructions), tuple(signals)

    def _node(self, schema: str) -> Node:
        try:
            return self._metadata.nodes_by_schema[schema]

        except KeyError:
            raise NodeNotFoundError(
                f"No node found for schema {schema!r}",
                detail=NodeErrorDetail(schema=schema),
            ) from None
