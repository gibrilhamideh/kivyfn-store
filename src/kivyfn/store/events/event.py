# src/kivyfn/store/events/event.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, TypeAlias

from kivyfn.store.operations import OpCode, OperationSource


Row: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class SetEvent:
    """A field change on a single schema."""

    opcode: ClassVar[OpCode] = OpCode.SET

    schema: str
    source: OperationSource
    key: str
    old: Any
    new: Any


@dataclass(frozen=True, slots=True, kw_only=True)
class SetRowEvent:
    """A field change on one row of a many schema."""

    opcode: ClassVar[OpCode] = OpCode.SETROW

    schema: str
    source: OperationSource
    id: str
    key: str
    old: Any
    new: Any


@dataclass(frozen=True, slots=True, kw_only=True)
class InsertEvent:
    """A row inserted into a many schema."""

    opcode: ClassVar[OpCode] = OpCode.INSERT

    schema: str
    source: OperationSource
    id: str
    new: Row


@dataclass(frozen=True, slots=True, kw_only=True)
class RemoveEvent:
    """A row removed from a many schema."""

    opcode: ClassVar[OpCode] = OpCode.REMOVE

    schema: str
    source: OperationSource
    id: str
    old: Row


StoreEvent: TypeAlias = SetEvent | SetRowEvent | InsertEvent | RemoveEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreCommit:
    """One atomic batch of canonical Store changes."""

    events: tuple[StoreEvent, ...]
    operation_id: str | None = None
    server_instance_id: str | None = None
    revision: int | None = None
