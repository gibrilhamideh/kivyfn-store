# src/kivyfn/store/schema/signal.py
from __future__ import annotations

from typing import Any, Generic, TypeVar

PayloadT = TypeVar("PayloadT")


class Signal(Generic[PayloadT]):
    """Declarative signal metadata; StateFactory creates callable emitters."""

    def __init__(self, *, undo: str | None = None) -> None:
        if undo is not None and (not isinstance(undo, str) or not undo):
            raise ValueError("Signal undo must be a non-empty signal name")
        self.name = ""
        self.undo = undo

    def __set_name__(self, owner: type[Any], name: str) -> None:
        if self.name and self.name != name:
            raise ValueError("A signal declaration cannot have multiple names")
        self.name = name

    def __set__(self, instance: object, value: Any) -> None:
        raise AttributeError("Schema signal metadata cannot be assigned")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
