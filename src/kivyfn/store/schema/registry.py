from __future__ import annotations
from typing import TYPE_CHECKING

from ..errors import SchemaError

if TYPE_CHECKING:
    from .schema import Schema


class SchemaRegistry:
    __registries__: dict[str, type[Schema]] = {}

    @classmethod
    def register(cls, schema: type[Schema]) -> None:
        name = schema.__name__

        if name in cls.__registries__:
            raise SchemaError(
                f"Duplicate schema name '{name}'. Schema names must be unique."
            )

        cls.__registries__[name] = schema

    @classmethod
    def get(cls, name: str) -> type[Schema]:
        try:
            return cls.__registries__[name]
        except KeyError:
            raise SchemaError(f"Schema '{name}' is not registered.")

    @classmethod
    def all(cls) -> dict[str, type[Schema]]:
        return cls.__registries__.copy()
