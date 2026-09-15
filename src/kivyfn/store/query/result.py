# src/kivyfn/store/query/result.py
from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True, init=False)
class QueryRow(Mapping[str, Any]):
    """One detached Query row; returned values never alias its snapshot."""

    id: str
    _values: Mapping[str, Any]

    def __init__(self, id: str, values: Mapping[str, Any]) -> None:
        if not isinstance(id, str) or not id:
            raise ValueError("Query row ID must be a non-empty string")

        if not isinstance(values, Mapping):
            raise TypeError("Query row values must be a mapping")

        object.__setattr__(self, "id", id)
        object.__setattr__(
            self, "_values", MappingProxyType(deepcopy(dict(values)))
        )

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._values[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(dict(self._values))


QueryResult: TypeAlias = tuple[QueryRow, ...]
