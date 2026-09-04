# kivyfn/store/schema/schema.py
from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from kivyfn.store.error import (
    FieldError,
    FieldErrorDetail,
    FieldNotFoundError,
)
from kivyfn.store.schema.fields import (
    BaseField,
    ForeignKeyField,
)
from kivyfn.store.schema.signal import Signal
from kivyfn.store.schema.trigger import Trigger


IndexKey = tuple[str, ...]


class Schema:
    many: ClassVar[bool] = False
    persistent: ClassVar[bool] = False
    persist_interval: ClassVar[str | None] = None

    indexes: ClassVar[list[IndexKey]] = []
    uniques: ClassVar[list[IndexKey]] = []

    def __init__(self) -> None:
        self.name = self.__class__.__name__

        self._fields: dict[str, BaseField] = {}
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
        for name, attr in self.__class__.__dict__.items():
            if isinstance(attr, BaseField):
                attr.name = name
                self._fields[name] = attr

    def _build_signals(self) -> None:
        for name, attr in self.__class__.__dict__.items():
            if isinstance(attr, Signal):
                attr.name = name
                self._signals[name] = attr

    def _build_triggers(self) -> None:
        for name, attr in self.__class__.__dict__.items():
            trigger_meta = getattr(
                attr,
                "_trigger_meta",
                None,
            )

            if not trigger_meta:
                continue

            bound_method = getattr(
                self,
                name,
            )

            for trigger in trigger_meta:
                self._triggers.append(
                    Trigger(
                        opcode=trigger.opcode,
                        field=trigger.field,
                        fn=bound_method,
                    )
                )

    def _build_indexes(self) -> None:
        for index in self.indexes:
            self._indexes.add(
                tuple(index),
            )

        for unique in self.uniques:
            key = tuple(unique)

            self._uniques.add(key)
            self._indexes.add(key)

        for name, field in self._fields.items():
            if isinstance(field, ForeignKeyField):
                self._indexes.add(
                    (name,),
                )

    def _validate_index_keys(self) -> None:
        field_names = set(self._fields)

        for key in self._indexes | self._uniques:
            for field_name in key:
                if field_name not in field_names:
                    raise ValueError(
                        f"Unknown field {field_name!r} "
                        f"in index or unique constraint "
                        f"for {self.name}"
                    )

    # -------------------------------------------------
    # Fields
    # -------------------------------------------------

    def get_field(
        self,
        name: str,
    ) -> BaseField:
        try:
            return self._fields[name]
        except KeyError:
            raise FieldNotFoundError(
                (
                    f"Field {name!r} does not exist "
                    f"in schema {self.name}"
                ),
                detail=FieldErrorDetail(
                    schema=self.name,
                    field=name,
                ),
            ) from None

    def fields(self) -> dict[str, BaseField]:
        return self._fields.copy()

    def field_names(self) -> set[str]:
        return set(self._fields)

    # -------------------------------------------------
    # Signals
    # -------------------------------------------------

    def get_signal(
        self,
        name: str,
    ) -> Signal[Any]:
        try:
            return self._signals[name]
        except KeyError:
            raise ValueError(
                f"Unknown signal {name!r} in {self.name}"
            ) from None

    def signals(self) -> dict[str, Signal[Any]]:
        return self._signals.copy()

    def signal_names(self) -> set[str]:
        return set(self._signals)

    def has_signal(
        self,
        name: str,
    ) -> bool:
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

    def foreign_key_fields(
        self,
    ) -> dict[str, ForeignKeyField]:
        return {
            name: field
            for name, field in self._fields.items()
            if isinstance(field, ForeignKeyField)
        }

    def is_foreignkey(
        self,
        name: str,
    ) -> bool:
        return name in self.foreign_key_fields()

    # -------------------------------------------------
    # Schema information
    # -------------------------------------------------

    def is_many(self) -> bool:
        return self.many

    def defaults(self) -> dict[str, Any]:
        return {
            name: getattr(
                field,
                "default",
                None,
            )
            for name, field in self._fields.items()
        }

    # -------------------------------------------------
    # Validation
    # -------------------------------------------------

    def challenge_field(
        self,
        name: str,
        value: Any,
    ) -> None:
        field = self.get_field(name)

        try:
            field.challenge(value)
        except FieldError as exc:
            detail = exc.detail

            if isinstance(detail, FieldErrorDetail):
                exc.detail = replace(
                    detail,
                    schema=self.name,
                    field=detail.field or name,
                )

            raise

    def challenge_fields(
        self,
        data: dict[str, Any],
    ) -> None:
        for name, value in data.items():
            self.challenge_field(
                name,
                value,
            )
