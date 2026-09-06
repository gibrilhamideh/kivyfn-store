# src/kivyfn/store/schema/signal.py
from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Protocol,
    Self,
    TypeVar,
    overload,
)


if TYPE_CHECKING:
    from kivyfn.store.state.operation import StateOperation


PayloadT = TypeVar(
    "PayloadT",
    contravariant=True,
)


class SignalCall(Protocol[PayloadT]):
    def __call__(
        self,
        data: PayloadT | None = None,
    ) -> StateOperation:
        ...


class Signal(Generic[PayloadT]):
    """
    Declarative schema signal.

    On a schema class, the signal is metadata.

    On a live state entity, the signal behaves like a callable action
    returning a StateOperation.
    """

    def __init__(self) -> None:
        self.name = ""

    def __set_name__(
        self,
        owner: type[Any],
        name: str,
    ) -> None:
        self.name = name

    @overload
    def __get__(
        self,
        instance: None,
        owner: type[Any],
    ) -> Self:
        ...

    @overload
    def __get__(
        self,
        instance: object,
        owner: type[Any],
    ) -> SignalCall[PayloadT]:
        ...

    def __get__(
        self,
        instance: object | None,
        owner: type[Any],
    ) -> Any:
        return self

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"name={self.name!r}>"
        )
