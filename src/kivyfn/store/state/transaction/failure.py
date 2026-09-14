# src/kivyfn/store/state/transaction/failure.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


TransactionFailureType: TypeAlias = Literal[
    "network",
    "field",
    "foreign_key",
    "trigger",
    "policy",
    "conflict",
    "not_found",
    "timeout",
    "internal",
]


@dataclass(frozen=True, slots=True)
class TransactionFailure:
    """Normalized failure for one State transaction."""

    type: TransactionFailureType
    message: str

    def __post_init__(self) -> None:
        if not self.message:
            raise ValueError(
                "Transaction failure message must be a non-empty string"
            )
