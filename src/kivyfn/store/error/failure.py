# kivyfn/store/error/data.py
from __future__ import annotations

from dataclasses import dataclass

from kivyfn.store.error.category import StoreErrorCategory
from kivyfn.store.error.code import StoreErrorCode
from kivyfn.store.error.context import StoreErrorContext
from kivyfn.store.error.detail import StoreErrorDetail
from kivyfn.store.error.status import StoreErrorStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreFailure:
    """
    Normalized public representation of a store failure.

    StoreFailure is transport-independent and is the error type exposed by
    failed Store results and Host responses.

    Application logic should use `code` for specific handling and `status` or
    `category` for broader fallback behavior. The message is intended for
    diagnostics and must not be treated as a stable machine-readable value.
    """

    code: StoreErrorCode
    category: StoreErrorCategory
    status: StoreErrorStatus
    message: str

    detail: StoreErrorDetail | None = None
    context: StoreErrorContext | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("Store error message must be a non-empty string")
