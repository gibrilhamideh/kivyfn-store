from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Union, Iterator

from ..errors import AddressError

PathPart = Union[str, int]


@dataclass(frozen=True)
class Address:
    parts: tuple[PathPart, ...]

    def __init__(self, *parts: PathPart):
        object.__setattr__(self, "parts", parts)

    # --- iteration / sequence-like ---
    def __iter__(self) -> Iterator[PathPart]:
        return iter(self.parts)

    def __len__(self) -> int:
        return len(self.parts)

    def __getitem__(self, index: int) -> PathPart:
        return self.parts[index]

    # --- Operators ---
    def __truediv__(self, part: PathPart) -> Address:
        """Append a child part (like pathlib.Path)."""
        return Address(*self.parts + (part,))

    # --- String repr ---
    def __str__(self) -> str:
        if not self.parts:
            return "<empty>"
        out = [str(self.parts[0])]
        for p in self.parts[1:]:
            out.append(f"[{p}]" if isinstance(p, int) else f".{p}")
        return "".join(out)

    def __repr__(self) -> str:
        return f"Address({self.parts})"

    def __hash__(self) -> int:
        return hash(self.parts)

    # --- Properties ---
    @property
    def relative(self) -> Address:
        """All parts inside the model (after root)."""
        if len(self.parts) <= 1:
            raise ValueError("Path has no relative part (only root)")
        return Address(*self.parts[1:])

    @property
    def root(self) -> str:
        """Model name (the first part)."""
        if not self.parts:
            raise ValueError("Empty path has no root (model)")
        return str(self.parts[0])

    @property
    def leaf(self) -> str:
        """Return the last element in the path."""
        if not self.parts:
            raise ValueError("Empty path has no leaf")
        return str(self.parts[-1])

    @property
    def parent(self) -> Address:
        """Return parent path. Root fields return empty Path for the document."""
        if not self.parts:
            return Address()  # no parent for empty path
        if len(self.parts) == 1:
            return Address()  # root document
        return Address(*self.parts[:-1])

    def prefixes(self) -> list[Address]:
        return [Address(*self.parts[:i]) for i in range(1, len(self.parts) + 1)]

    def walk(self, data: Mapping) -> Any:
        try:
            for point in self.parts:
                data = data[point]

            return data
        except Exception:
            raise AddressError
