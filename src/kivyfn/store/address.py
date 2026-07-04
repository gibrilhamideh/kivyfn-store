# kivyfn/store/address.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Iterator, Mapping, TypeVar


T_co = TypeVar("T_co", covariant=True)

PathPart = str


@dataclass(slots=True, frozen=True)
class Address(Generic[T_co]):
    parts: tuple[PathPart, ...]

    def __init__(self, *parts: PathPart) -> None:
        object.__setattr__(self, "parts", parts)

    # --- iteration / sequence-like ---

    def __iter__(self) -> Iterator[PathPart]:
        return iter(self.parts)

    def __len__(self) -> int:
        return len(self.parts)

    def __getitem__(self, index: int) -> PathPart:
        return self.parts[index]

    # --- operators ---

    def __truediv__(self, part: PathPart) -> Address[Any]:
        """
        Append a child part.

        The resulting value type is unknown unless the caller explicitly
        annotates the new address.
        """
        return Address(*self.parts, part)

    # --- string representation ---

    def __str__(self) -> str:
        if not self.parts:
            return "<empty>"

        root, *remaining = self.parts
        return str(root) + "".join(f".{part}" for part in remaining)

    def __repr__(self) -> str:
        return f"Address({self.parts!r})"

    # --- properties ---

    @property
    def relative(self) -> Address[Any]:
        """Return all parts after the root."""
        if len(self.parts) <= 1:
            raise ValueError("Address has no relative part")

        return Address(*self.parts[1:])

    @property
    def root(self) -> str:
        """Return the first address part."""
        if not self.parts:
            raise ValueError("Empty address has no root")

        return self.parts[0]

    @property
    def leaf(self) -> str:
        """Return the final address part."""
        if not self.parts:
            raise ValueError("Empty address has no leaf")

        return self.parts[-1]

    @property
    def parent(self) -> Address[Any]:
        """Return the parent address."""
        if len(self.parts) <= 1:
            return Address()

        return Address(*self.parts[:-1])

    def prefixes(self) -> list[Address[Any]]:
        return [
            Address(*self.parts[:index])
            for index in range(1, len(self.parts) + 1)
        ]

    def walk(self, data: Mapping[str, Any]) -> Any:
        current: Any = data

        try:
            for part in self.parts:
                current = current[part]

        except KeyError as exc:
            raise ValueError(
                f"Address part {exc.args[0]!r} was not found in {self}"
            ) from exc

        except TypeError as exc:
            raise ValueError(
                f"Cannot traverse through a non-mapping value at {self}"
            ) from exc

        return current

    def to_list(self) -> list[PathPart]:
        return list(self.parts)
