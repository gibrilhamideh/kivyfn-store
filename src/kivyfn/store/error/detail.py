# kivyfn/store/error/detail.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

ProtocolValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | list["ProtocolValue"]
    | dict[str, "ProtocolValue"]
)

StoreAddress: TypeAlias = tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreErrorDetail:
    """
    Base type for structured store error details.

    Detail objects contain semantic information about a failure. They remain
    transport-independent and immutable. The codec converts tuple values to
    protocol lists when serializing them.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class InstructionErrorDetail(StoreErrorDetail):
    opcode: str | None = None
    address: StoreAddress | None = None
    row_id: str | None = None
    field: str | None = None
    value: ProtocolValue = None
    source: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AddressErrorDetail(StoreErrorDetail):
    address: StoreAddress
    segment: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeErrorDetail(StoreErrorDetail):
    address: StoreAddress | None = None
    schema: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldErrorDetail(StoreErrorDetail):
    schema: str | None = None
    field: str | None = None
    value: ProtocolValue = None
    rule: str | None = None
    expected: ProtocolValue = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    allow_none: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RowErrorDetail(StoreErrorDetail):
    schema: str | None = None
    address: StoreAddress | None = None
    row_id: str | None = None
    operation: str | None = None
    conflicting_operation: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexErrorDetail(StoreErrorDetail):
    schema: str | None = None
    fields: tuple[str, ...] = ()
    value: ProtocolValue = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConstraintErrorDetail(StoreErrorDetail):
    schema: str | None = None
    fields: tuple[str, ...] = ()
    value: ProtocolValue = None

    row_id: str | None = None
    existing_row_id: str | None = None

    reference_schema: str | None = None
    reference_field: str | None = None

    referencing_schema: str | None = None
    referencing_field: str | None = None
    referencing_row_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeErrorDetail(StoreErrorDetail):
    store: str | None = None
    operation: str | None = None
    transport: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryErrorDetail(StoreErrorDetail):
    entry_id: str | None = None
    schema: str | None = None
    row_id: str | None = None
    field: str | None = None
    expected: ProtocolValue = None
    value: ProtocolValue = None
