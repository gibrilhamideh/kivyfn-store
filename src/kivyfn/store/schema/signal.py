# kivyfn/store/signals/signal.py
from __future__ import annotations

from typing import Generic, TypeVar


T = TypeVar("T")


class Signal(Generic[T]):
    """
    Declarative schema signal.

    Signals are declared on Schema classes and discovered when a Schema
    instance is built.

    Example:

        class AlarmSchema(Schema):
            triggered = Signal[dict[str, object]]()
    """

    def __init__(self) -> None:
        self.name = ""

    def __set_name__(
        self,
        owner: type[object],
        name: str,
    ) -> None:
        self.name = name

    def __repr__(
        self,
    ) -> str:
        return (
            f"<{type(self).__name__} "
            f"name={self.name!r}>"
        )
