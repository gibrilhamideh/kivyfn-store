# kivyfn/store/error/category.py
from __future__ import annotations

from enum import StrEnum


class StoreErrorCategory(StrEnum):
    """
    Broad semantic category for a store failure.

    Categories group related error codes for generic handling, logging,
    metrics, and fallback UI behavior. Specific application behavior should
    normally use StoreErrorCode.
    """

    INSTRUCTION = "instruction"
    ADDRESS = "address"
    FIELD = "field"
    ROW = "row"
    INDEX = "index"
    CONSTRAINT = "constraint"
    STATE = "state"
    AVAILABILITY = "availability"
    PROTOCOL = "protocol"
    INTERNAL = "internal"
