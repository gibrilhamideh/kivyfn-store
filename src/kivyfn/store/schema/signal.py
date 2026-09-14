# src/kivyfn/store/schema/signal.py
from __future__ import annotations

from typing import Any, Generic, TypeVar


PayloadT = TypeVar("PayloadT")


class Signal(Generic[PayloadT]):
    """Declarative schema signal metadata."""

    def __init__(self) -> None:
        self.name = ""

    def __set_name__(self, owner: type[Any], name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
