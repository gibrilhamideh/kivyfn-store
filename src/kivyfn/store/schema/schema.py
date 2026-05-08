from __future__ import annotations

from typing import ClassVar

from kivyfn.store.schema.trigger import Trigger
from kivyfn.store.schema.fields import BaseField, ForeignKeyField
from kivyfn.store.errors.store import FieldNotFoundError


IndexKey = tuple[str, ...]


class Schema:
    many: ClassVar[bool] = False
    persistent: ClassVar[bool] = False

    indexes: ClassVar[list[IndexKey]] = []
    uniques: ClassVar[list[IndexKey]] = []

    def __init__(self) -> None:
        self.name = self.__class__.__name__

        self._fields: dict[str, BaseField] = {}
        self._triggers: list[Trigger] = []

        self._indexes: set[IndexKey] = set()
        self._uniques: set[IndexKey] = set()

        self._build_fields()
        self._build_triggers()
        self._build_indexes()
        self._validate_index_keys()

    def _build_fields(self) -> None:
        for name, attr in self.__class__.__dict__.items():
            if isinstance(attr, BaseField):
                attr.name = name
                self._fields[name] = attr

    def _build_triggers(self) -> None:

        for name, attr in self.__class__.__dict__.items():
            trigger_meta = getattr(attr, "_trigger_meta", None)
            if not trigger_meta:
                continue

            bound_method = getattr(self, name)

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
            self._indexes.add(tuple(index))

        for unique in self.uniques:
            key = tuple(unique)
            self._uniques.add(key)
            self._indexes.add(key)

        for name, field in self._fields.items():
            if isinstance(field, ForeignKeyField):
                self._indexes.add((name,))

    def _validate_index_keys(self) -> None:
        names = set(self._fields)

        for key in self._indexes | self._uniques:
            for field_name in key:
                if field_name not in names:
                    raise ValueError(
                        f"Unknown field '{field_name}' in index/unique for {self.name}"
                    )

    def get_field(self, name: str) -> BaseField:
        try:
            return self._fields[name]
        except KeyError:
            raise FieldNotFoundError(f"Unknown field '{name}' in {self.name}")

    def fields(self) -> dict[str, BaseField]:
        return self._fields.copy()

    def field_names(self) -> set[str]:
        return set(self._fields)

    def triggers(self) -> list[Trigger]:
        return self._triggers.copy()

    def index_keys(self) -> set[IndexKey]:
        return set(self._indexes)

    def unique_keys(self) -> set[IndexKey]:
        return set(self._uniques)

    def foreign_key_fields(self) -> dict[str, ForeignKeyField]:
        return {
            name: field
            for name, field in self._fields.items()
            if isinstance(field, ForeignKeyField)
        }

    def is_many(self) -> bool:
        return self.many

    def is_foreignkey(self, name: str) -> bool:
        return name in self.foreign_key_fields()

    def defaults(self) -> dict:
        return {
            name: getattr(field, "default", None)
            for name, field in self._fields.items()
        }

    def challenge_field(self, name: str, value) -> None:
        field = self.get_field(name)
        field.challenge(value)

    def challenge_fields(self, data: dict) -> None:
        for name, value in data.items():
            self.challenge_field(name, value)
