from __future__ import annotations

from typing import ClassVar

from .registry import SchemaRegistry
from .trigger import Trigger

from ..schema.fields import BaseField, ForeignKeyField
from ..core.registry import ForeignKeyRegistry
from ..errors import FieldNotFoundError


IndexKey = tuple[str, ...]


class Schema:
    many: ClassVar[bool] = False
    persistent: ClassVar[bool] = False

    indexes: ClassVar[list[IndexKey]] = []
    uniques: ClassVar[list[IndexKey]] = []

    __fields__: ClassVar[dict[str, BaseField]]
    __triggers__: ClassVar[list[Trigger]]
    __indexes__: ClassVar[set[IndexKey]]
    __uniques__: ClassVar[set[IndexKey]]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()

        fields: dict[str, BaseField] = {}
        triggers: list[Trigger] = []

        for name, attr in cls.__dict__.items():
            if isinstance(attr, BaseField):
                attr.name = name
                fields[name] = attr

                if isinstance(attr, ForeignKeyField):
                    ForeignKeyRegistry.register(
                        schema=cls.__name__,
                        field=name,
                        reference=attr.schema,
                        on_delete=attr.on_delete,
                    )

            if hasattr(attr, "_trigger_meta"):
                triggers.extend(attr._trigger_meta)  # pyright: ignore

        cls.__fields__ = fields
        cls.__triggers__ = triggers

        cls.__indexes__ = set()
        cls.__uniques__ = set()

        # explicit non-unique indexes
        for index in getattr(cls, "indexes", []):
            cls.__indexes__.add(tuple(index))

        # unique constraints are automatically indexes
        for unique in getattr(cls, "uniques", []):
            key = tuple(unique)
            cls.__uniques__.add(key)
            cls.__indexes__.add(key)

        # foreign keys are automatically indexed
        for name, field in fields.items():
            if isinstance(field, ForeignKeyField):
                cls.__indexes__.add((name,))

        cls._validate_index_keys()
        SchemaRegistry.register(cls)

    @classmethod
    def _validate_index_keys(cls) -> None:
        names = set(cls.__fields__)

        for key in cls.__indexes__ | cls.__uniques__:
            for field_name in key:
                if field_name not in names:
                    raise FieldNotFoundError(
                        f"Unknown field '{field_name}' in index/unique for {cls.__name__}"
                    )

    @classmethod
    def get_field(cls, name: str) -> BaseField:
        try:
            return cls.__fields__[name]
        except KeyError:
            raise FieldNotFoundError(f"Unknown field '{name}' in {cls.__name__}")

    @classmethod
    def fields(cls) -> dict[str, BaseField]:
        return cls.__fields__.copy()

    @classmethod
    def field_names(cls) -> set[str]:
        return set(cls.__fields__)

    @classmethod
    def index_keys(cls) -> set[IndexKey]:
        return set(cls.__indexes__)

    @classmethod
    def unique_keys(cls) -> set[IndexKey]:
        return set(cls.__uniques__)

    @classmethod
    def foreign_key_fields(cls) -> tuple[str, ...]:
        return tuple(
            name
            for name, field in cls.__fields__.items()
            if isinstance(field, ForeignKeyField)
        )

    @classmethod
    def is_many(cls) -> bool:
        return cls.many

    @classmethod
    def is_foreignkey(cls, field) -> bool:
        return field in cls.foreign_key_fields()

    @classmethod
    def defaults(cls) -> dict:
        return {
            name: getattr(field, "default", None)
            for name, field in cls.__fields__.items()
        }

    @classmethod
    def challenge_field(cls, name: str, value) -> None:
        field = cls.get_field(name)
        field.challenge(value)

    @classmethod
    def challenge_fields(cls, data: dict) -> None:
        for name, value in data.items():
            cls.challenge_field(name, value)
