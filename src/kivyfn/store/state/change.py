from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias


StateChangeKind: TypeAlias = Literal[
    "set",
    "insert",
    "remove",
]

StatePath: TypeAlias = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StateChange:
    """
    Describe one change applied to observable state.

    Paths are relative to the state adapter:

    - A single-schema field uses ``(key,)``.
    - A many-schema row uses ``(id,)``.
    - A many-schema field uses ``(id, key)``.

    Insertions have no old value and removals have no new value. The
    change kind distinguishes absence from a stored value of ``None``.
    """

    kind: StateChangeKind
    path: StatePath
    old_value: Any
    new_value: Any
