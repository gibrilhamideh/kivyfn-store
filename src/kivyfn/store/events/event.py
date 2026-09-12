# src/kivyfn/store/events/event.py
from __future__ import annotations

from dataclasses import dataclass, field
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
    """One atomic batch of canonical Store changes.

    schema_revisions contains each changed schema's in-memory revision
    after this commit. Each affected schema advances once, regardless of
    how many events it contributes. Revisions are comparable only within
    the same server_instance_id.

    Store assigns the instance ID and schema revisions after successful
    execution. Engine-created commits start with no instance ID and an
    empty mapping. Successful no-op commits also have an empty mapping
    and are not published. There is no global revision or replay history.

    Subscription messages may filter events while keeping revisions for
    changed query dependencies whose raw events are not included.
    """

    events: tuple[StoreEvent, ...]
    operation_id: str | None = None
    server_instance_id: str | None = None
    schema_revisions: dict[str, int] = field(default_factory=dict)
