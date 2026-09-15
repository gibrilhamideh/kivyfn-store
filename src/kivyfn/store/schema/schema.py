# src/kivyfn/store/schema/schema.py
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, ClassVar, get_args

from kivyfn.store.error import (
    FieldError,
    FieldErrorDetail,
    FieldNotFoundError,
    InstructionErrorDetail,
    InvalidOperationError,
)
from kivyfn.store.operations import OpCode
from kivyfn.store.schema.fields import BaseField, ForeignKeyField
from kivyfn.store.schema.signal import Signal
from kivyfn.store.schema.trigger import Trigger

IndexKey = tuple[str, ...]


class Schema:
    persistent: ClassVar[bool] = False
    track_history: ClassVar[bool] = False

    indexes: ClassVar[list[IndexKey]] = []
    uniques: ClassVar[list[IndexKey]] = []

    def __init__(self) -> None:
        declarations = _declarations(type(self))
        for name, declaration in declarations.items():
            if isinstance(declaration, (BaseField, Signal)) and (
                name in Schema.__dict__
                or name
                in {
                    "name",
                    "_fields",
                    "_signals",
                    "_triggers",
                    "_indexes",
                    "_uniques",
                }
            ):
                raise ValueError(
                    f"Schema declaration name {name!r} is reserved"
                )
        if type(self.track_history) is not bool:
            raise TypeError("Schema.track_history must be a bool")
        self.name = self.__class__.__name__

        self._fields: dict[str, BaseField[Any]] = {}
        self._signals: dict[str, Signal[Any]] = {}
        self._triggers: list[Trigger] = []

        self._indexes: set[IndexKey] = set()
        self._uniques: set[IndexKey] = set()

        self._build_fields()
        self._build_signals()
        self._build_triggers()
        self._build_indexes()
        self._validate_index_keys()

    # -------------------------------------------------
    # Build
    # -------------------------------------------------

    def _build_fields(self) -> None:
        for name, attr in _declarations(type(self)).items():
            if isinstance(attr, BaseField):
                self._fields[name] = attr

    def _build_signals(self) -> None:
        for name, attr in _declarations(type(self)).items():
            if isinstance(attr, Signal):
                self._signals[name] = attr
        for name, signal in self._signals.items():
            if signal.undo is not None and signal.undo not in self._signals:
                raise ValueError(
                    f"Invalid inverse {signal.undo!r} for {self.name}.{name}"
                )
            if signal.undo is not None:
                inverse = self._signals[signal.undo]
                contract = get_args(getattr(signal, "__orig_class__", None))
                other = get_args(getattr(inverse, "__orig_class__", None))
                if contract and other and contract != other:
                    raise ValueError(
                        f"Inverse payload contracts differ for "
                        f"{self.name}.{name}"
                    )

    def _build_triggers(self) -> None:
        for name, attr in _declarations(type(self)).items():
            trigger_meta = getattr(attr, "_trigger_meta", None)

            if not trigger_meta:
                continue

            bound_method = getattr(self, name)

            for trigger in trigger_meta:
                if trigger.field is not None:
                    if trigger.opcode not in {OpCode.SET, OpCode.SETROW}:
                        raise ValueError(
                            "Only field writes accept field triggers"
                        )
                    if trigger.field not in self._fields:
                        raise ValueError(
                            f"Unknown trigger field {trigger.field!r} "
                            f"in {self.name}"
                        )
                self._triggers.append(
                    Trigger(
                        opcode=trigger.opcode,
                        field=trigger.field,
                        fn=bound_method,
                    )
                )

    def _build_indexes(self) -> None:
        for index in self.indexes:
            self._indexes.add(_index_key(index))

        for unique in self.uniques:
            key = _index_key(unique)

            self._uniques.add(key)
            self._indexes.add(key)

        for name, field in self._fields.items():
            if isinstance(field, ForeignKeyField):
                self._indexes.add((name,))

    def _validate_index_keys(self) -> None:
        field_names = set(self._fields)

        for key in self._indexes | self._uniques:
            for field_name in key:
                if field_name not in field_names:
                    raise ValueError(
                        f"Unknown field {field_name!r} "
                        "in index or unique constraint "
                        f"for {self.name}"
                    )

    # -------------------------------------------------
    # Fields
    # -------------------------------------------------

    def get_field(self, name: str) -> BaseField[Any]:
        try:
            return self._fields[name]

        except KeyError:
            raise FieldNotFoundError(
                f"Field {name!r} does not exist in schema {self.name}",
                detail=FieldErrorDetail(schema=self.name, field=name),
            ) from None

    def fields(self) -> dict[str, BaseField[Any]]:
        return self._fields.copy()

    def field_names(self) -> set[str]:
        return set(self._fields)

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    def get_signal(self, name: str) -> Signal[Any]:
        try:
            return self._signals[name]

        except KeyError:
            raise InvalidOperationError(
                f"Unknown signal {name!r} in {self.name}",
                detail=InstructionErrorDetail(field=name),
            ) from None

    def signals(self) -> dict[str, Signal[Any]]:
        return self._signals.copy()

    def signal_names(self) -> set[str]:
        return set(self._signals)

    def has_signal(self, name: str) -> bool:
        return name in self._signals

    # -------------------------------------------------
    # Triggers
    # -------------------------------------------------

    def triggers(self) -> list[Trigger]:
        return self._triggers.copy()

    # -------------------------------------------------
    # Indexes
    # -------------------------------------------------

    def index_keys(self) -> set[IndexKey]:
        return set(self._indexes)

    def unique_keys(self) -> set[IndexKey]:
        return set(self._uniques)

    # -------------------------------------------------
    # Foreign keys
    # -------------------------------------------------

    def foreign_key_fields(self) -> dict[str, ForeignKeyField]:
        return {
            name: field
            for name, field in self._fields.items()
            if isinstance(field, ForeignKeyField)
        }

    def is_foreignkey(self, name: str) -> bool:
        return name in self.foreign_key_fields()

    # -------------------------------------------------
    # Schema information
    # -------------------------------------------------

    def defaults(self) -> dict[str, Any]:
        return deepcopy(
            {
                name: getattr(field, "default", None)
                for name, field in self._fields.items()
            }
        )

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    def challenge_field(self, name: str, value: Any) -> None:
        field = self.get_field(name)

        try:
            field.challenge(value)

        except FieldError as error:
            detail = error.detail

            if isinstance(detail, FieldErrorDetail):
                error.detail = replace(
                    detail, schema=self.name, field=detail.field or name
                )

            raise

    def challenge_fields(self, data: dict[str, Any]) -> None:
        for name, value in data.items():
            self.challenge_field(name, value)


class SingleSchema(Schema):
    """Schema containing one canonical record."""


class ManySchema(Schema):
    """Schema containing canonical records keyed by row ID."""


def _declarations(schema_type: type[Schema]) -> dict[str, Any]:
    """Resolve declarations with normal Python inheritance and overrides."""
    declarations: dict[str, Any] = {}
    for base in reversed(schema_type.__mro__):
        declarations.update(vars(base))
    return declarations


def _index_key(value: IndexKey) -> IndexKey:
    if not isinstance(value, (tuple, list)) or not value:
        raise ValueError("An index requires a non-empty sequence of fields")
    if not all(isinstance(name, str) and name for name in value):
        raise ValueError("Index fields must be non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("An index cannot repeat the same field")
    return tuple(value)
