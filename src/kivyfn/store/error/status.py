# kivyfn/store/error/status.py
from __future__ import annotations

from enum import StrEnum


class StoreErrorStatus(StrEnum):
    """
    Broad transport-independent status for a store failure.

    Status values classify the general outcome of an operation. Application
    logic should normally use StoreErrorCode for specific handling and use
    StoreErrorStatus for generic fallback behavior.
    """

    INVALID = "invalid"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    INTERNAL = "internal"
