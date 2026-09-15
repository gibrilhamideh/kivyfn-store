from __future__ import annotations

from typing import Any, Generic, Self, TypeVar, overload

ValueT = TypeVar("ValueT")


class BaseField(Generic[ValueT]):
    """Base descriptor for declarative schema fields.

    The field object remains schema metadata at runtime. The descriptor
    typing describes the value exposed by a live state entity so the schema
    can also act as the static shape of that entity.
    """

    def __init__(self) -> None:
        self.name = ""

    def __set_name__(self, owner: type[Any], name: str) -> None:
        if self.name and self.name != name:
            raise ValueError("A field declaration cannot have multiple names")
        self.name = name

    @overload
    def __get__(self, instance: None, owner: type[Any]) -> Self: ...

    @overload
    def __get__(self, instance: object, owner: type[Any]) -> ValueT: ...

    def __get__(self, instance: object | None, owner: type[Any]) -> Any:
        return self

    def __set__(self, instance: object, value: ValueT) -> None:
        raise AttributeError(
            "Schema field metadata cannot be assigned directly"
        )

    @property
    def metadata(self) -> dict[str, Any]:
        return {}

    def challenge(self, value: Any) -> None:
        pass
