# src/kivyfn/store/state/failure.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


StateFailureType: TypeAlias = Literal[
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
    """Normalized failure exposed by the reactive State API."""

    type: StateFailureType
    message: str

