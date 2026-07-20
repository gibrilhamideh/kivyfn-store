# kivyfn/store/node/index.py
from __future__ import annotations

from typing import Any, Literal, overload

from kivyfn.store.error import (
    ConstraintErrorDetail,
    IndexErrorDetail,
    IndexNotFoundError,
    UniqueConstraintError,
)


IndexKey = tuple[str, ...]


class Index:
    def __init__(
        self,
        fields: IndexKey,
        *,
        schema: str,
        unique: bool = False,
    ) -> None:
        self.fields = fields
        self.schema = schema
        self.unique = unique

        self._map: dict[Any, set[str]] = {}

    def build_key(
        self,
        row: dict[str, Any],
    ) -> Any:
        if len(self.fields) == 1:
            return row.get(
                self.fields[0],
            )

        return tuple(
            row.get(field)
            for field in self.fields
        )

    def add(
        self,
        row_id: str,
        row: dict[str, Any],
    ) -> None:
        key = self.build_key(
            row,
        )

        if key is None:
            return

        rows = self._map.setdefault(
            key,
            set(),
        )

        if (
            self.unique
            and rows
            and row_id not in rows
        ):
            raise self._unique_error(
                row_id=row_id,
                key=key,
                existing_row_id=next(iter(rows)),
            )

        rows.add(
            row_id,
        )

    def remove(
        self,
        row_id: str,
        row: dict[str, Any],
    ) -> None:
        key = self.build_key(
            row,
        )

        if key is None:
            return

        rows = self._map.get(
            key,
        )

        if not rows:
            return

        rows.discard(
            row_id,
        )

        if not rows:
            del self._map[key]

    def update(
        self,
        row_id: str,
        old_row: dict[str, Any],
        new_row: dict[str, Any],
    ) -> None:
        old_key = self.build_key(
            old_row,
        )

        new_key = self.build_key(
            new_row,
        )

        if old_key == new_key:
            return

        self._validate_add(
            row_id,
            new_key,
        )

        self.remove(
            row_id,
            old_row,
        )

        self.add(
            row_id,
            new_row,
        )

    def lookup(
        self,
        value: Any,
        *,
        first: bool = False,
    ) -> set[str] | str | None:
        rows = self._map.get(
            value,
        )

        if not rows:
            return None if first else set()

        if first:
            return next(
                iter(rows),
            )

        return set(
            rows,
        )

    def _validate_add(
        self,
        row_id: str,
        key: Any,
    ) -> None:
        if key is None:
            return

        rows = self._map.get(
            key,
        )

        if (
            self.unique
            and rows
            and row_id not in rows
        ):
            raise self._unique_error(
                row_id=row_id,
                key=key,
                existing_row_id=next(iter(rows)),
            )

    def _unique_error(
        self,
        *,
        row_id: str,
        key: Any,
        existing_row_id: str,
    ) -> UniqueConstraintError:
        return UniqueConstraintError(
            (
                "Unique constraint violated on "
                f"{self.schema}.{'.'.join(self.fields)}"
            ),
            detail=ConstraintErrorDetail(
                schema=self.schema,
                fields=self.fields,
                value=key,
                row_id=row_id,
                existing_row_id=existing_row_id,
            ),
        )


class IndexManager:
    def __init__(
        self,
        schema: Any,
    ) -> None:
        self._schema = schema.name
        self._indexes: dict[IndexKey, Index] = {}

        unique_keys = schema.unique_keys()

        for fields in schema.index_keys():
            self._indexes[fields] = Index(
                fields,
                schema=self._schema,
                unique=fields in unique_keys,
            )

    def add(
        self,
        row_id: str,
        row: dict[str, Any],
    ) -> None:
        # Validate all indexes first so partial mutation cannot occur.
        for index in self._indexes.values():
            key = index.build_key(
                row,
            )

            index._validate_add(
                row_id,
                key,
            )

        for index in self._indexes.values():
            index.add(
                row_id,
                row,
            )

    def remove(
        self,
        row_id: str,
        row: dict[str, Any],
    ) -> None:
        for index in self._indexes.values():
            index.remove(
                row_id,
                row,
            )

    def update(
        self,
        row_id: str,
        old_row: dict[str, Any],
        new_row: dict[str, Any],
    ) -> None:
        # Validate all new keys first so partial mutation cannot occur.
        for index in self._indexes.values():
            old_key = index.build_key(
                old_row,
            )

            new_key = index.build_key(
                new_row,
            )

            if old_key != new_key:
                index._validate_add(
                    row_id,
                    new_key,
                )

        for index in self._indexes.values():
            index.update(
                row_id,
                old_row,
                new_row,
            )

    @overload
    def lookup(
        self,
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: Literal[True],
    ) -> str | None:
        ...

    @overload
    def lookup(
        self,
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: Literal[False] = False,
    ) -> set[str]:
        ...

    def lookup(
        self,
        fields: str | tuple[str, ...],
        value: Any,
        *,
        first: bool = False,
    ) -> set[str] | str | None:
        key = self._normalize_fields(
            fields,
        )

        index = self._indexes.get(
            key,
        )

        if index is None:
            raise IndexNotFoundError(
                (
                    "No index exists for "
                    f"{self._schema}.{'.'.join(key)}"
                ),
                detail=IndexErrorDetail(
                    schema=self._schema,
                    fields=key,
                    value=value,
                ),
            )

        return index.lookup(
            value,
            first=first,
        )

    def _normalize_fields(
        self,
        fields: str | tuple[str, ...],
    ) -> IndexKey:
        if isinstance(fields, str):
            return (fields,)

        return tuple(
            fields,
        )
