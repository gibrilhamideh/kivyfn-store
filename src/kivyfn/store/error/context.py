# kivyfn/store/error/context.py
from __future__ import annotations

from dataclasses import dataclass

from kivyfn.store.error.detail import ProtocolValue, StoreAddress
from kivyfn.store.error.stage import StoreErrorStage


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreErrorContext:
    """
    Transport-safe diagnostic context describing where a store failure
    occurred.

    Context identifies where the failure was detected. Semantic information
    describing the failure belongs in the corresponding detail object.
    """

    stage: StoreErrorStage | None = None

    schema: str | None = None
    address: StoreAddress | None = None

    opcode: str | None = None
    source: str | None = None

    row_id: str | None = None
    field: str | None = None
    value: ProtocolValue = None

    instruction_index: int | None = None
