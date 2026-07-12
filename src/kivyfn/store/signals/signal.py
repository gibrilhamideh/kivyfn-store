from __future__ import annotations

from typing import Generic, TypeVar


T = TypeVar("T")


class Signal(Generic[T]):
    def __init__(self) -> None:
        self.name: str = ""

    def __repr__(self) -> str:
        return f"Signal(name={self.name!r})"
