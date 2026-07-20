# kivyfn/store/error/stage.py
from __future__ import annotations

from enum import StrEnum


class StoreErrorStage(StrEnum):
    """
    Stage at which a store operation failed.

    Stages describe where the failure was detected. They do not describe the
    semantic cause of the failure; StoreErrorCode provides that information.
    """

    REQUEST = "request"
    RESOLVE = "resolve"
    COMPILE = "compile"
    DELTA = "delta"
    VALIDATE = "validate"
    EXECUTE = "execute"
    PERSIST = "persist"
    DISPATCH = "dispatch"
