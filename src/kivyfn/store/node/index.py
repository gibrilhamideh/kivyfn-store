# src/kivyfn/store/node/index.py
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeAlias

from kivyfn.store.error import (
    ConstraintErrorDetail,
    FieldErrorDetail,
    FieldValidationError,
    IndexErrorDetail,
    IndexNotFoundError,
    UniqueConstraintError,
)

if TYPE_CHECKING:
    from kivyfn.store.schema import Schema

IndexKey: TypeAlias = tuple[str, ...]


class _Index:
    """One internal Store index."""

    def __init__(self, fields: IndexKey, *, schema: str, unique: bool) -> None:
        self.fields = fields
        self.schema = schema
        self.unique = unique
        self._rows: dict[Any, set[str]] = {}

    def key(self, row: Mapping[str, Any]) -> Any | None:
        values = tuple(row.get(field) for field in self.fields)
        if any(value is None for value in values):
            return None
        key = values[0] if len(values) == 1 else values
        try:
            hash(key)
        except TypeError:
            raise FieldValidationError(
                f"Indexed fields {self.schema}.{'.'.join(self.fields)} "
                "must contain hashable values",
                detail=FieldErrorDetail(
                    schema=self.schema,
                    field=".".join(self.fields),
                    rule="index",
                    expected="hashable value",
                ),
            ) from None
        return key

    def validate(self, row_id: str, key: Any | None) -> None:
        if key is None or not self.unique:
            return

        rows = self._rows.get(key)

        if not rows or row_id in rows:
            return

        raise UniqueConstraintError(
            f"Unique constraint violated on "
            f"{self.schema}.{'.'.join(self.fields)}",
            detail=ConstraintErrorDetail(
                schema=self.schema,
                fields=self.fields,
                value=key,
                row_id=row_id,
                existing_row_id=next(iter(rows)),
            ),
        )

    def add(self, row_id: str, key: Any | None) -> None:
        if key is None:
            return

        self._rows.setdefault(key, set()).add(row_id)

    def remove(self, row_id: str, key: Any | None) -> None:
        if key is None:
            return

        rows = self._rows.get(key)

        if rows is None:
            return

        rows.discard(row_id)

        if not rows:
            del self._rows[key]

    def lookup_all(self, value: Any) -> frozenset[str]:
        rows = self._rows.get(value)

        if not rows:
            return frozenset()

        return frozenset(rows)

    def lookup_one(self, value: Any) -> str | None:
        rows = self._rows.get(value)

        if not rows:
            return None

        return next(iter(rows))


class IndexManager:
    """Indexes one schema's many-row Store data."""

    def __init__(self, schema: Schema) -> None:
        self._schema = schema.name

        unique_keys = schema.unique_keys()

        self._indexes = {
            fields: _Index(
                fields, schema=self._schema, unique=fields in unique_keys
            )
            for fields in schema.index_keys()
        }

    def add(self, row_id: str, row: Mapping[str, Any]) -> None:
        keys = {index: index.key(row) for index in self._indexes.values()}

        for index, key in keys.items():
            index.validate(row_id, key)

        for index, key in keys.items():
            index.add(row_id, key)

    def remove(self, row_id: str, row: Mapping[str, Any]) -> None:
        for index in self._indexes.values():
            index.remove(row_id, index.key(row))

    def update(
        self,
        row_id: str,
        old_row: Mapping[str, Any],
        new_row: Mapping[str, Any],
    ) -> None:
        changed: list[tuple[_Index, Any | None, Any | None]] = []

        for index in self._indexes.values():
            old_key = index.key(old_row)
            new_key = index.key(new_row)

            if old_key == new_key:
                continue

            index.validate(row_id, new_key)

            changed.append((index, old_key, new_key))

        for index, old_key, new_key in changed:
            index.remove(row_id, old_key)
            index.add(row_id, new_key)

    def lookup_all(self, fields: str | IndexKey, value: Any) -> frozenset[str]:
        return self._index(fields, value).lookup_all(value)

    def lookup_one(self, fields: str | IndexKey, value: Any) -> str | None:
        return self._index(fields, value).lookup_one(value)

    def _index(self, fields: str | IndexKey, value: Any) -> _Index:
        key = (fields,) if isinstance(fields, str) else fields

        index = self._indexes.get(key)

        if index is None:
            raise IndexNotFoundError(
                f"No index exists for {self._schema}.{'.'.join(key)}",
                detail=IndexErrorDetail(
                    schema=self._schema, fields=key, value=value
                ),
            )

        return index
