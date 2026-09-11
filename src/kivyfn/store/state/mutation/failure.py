# src/kivyfn/store/state/mutation/failure.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


StateFailureType = Literal[
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
class StateFailure:
    """Normalized failure exposed through State mutations."""

    type: StateFailureType
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message:
            raise ValueError(
                "State failure message must be a non-empty string"
            )
