
# src/kivyfn/store/address.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(
    frozen=True,
    slots=True,
    init=False,
)
class Address:
    """Immutable internal path identifying a Store node."""

    parts: tuple[str, ...]

    def __init__(
        self,
        *parts: str,
    ) -> None:
        object.__setattr__(
            self,
            "parts",
            parts,
        )

    def __str__(self) -> str:
        if not self.parts:
            return "<empty>"

        return ".".join(
            self.parts,
        )

    def __repr__(self) -> str:
        return f"Address({self.parts!r})"

    def to_list(self) -> list[str]:
        return list(
            self.parts,
        )

