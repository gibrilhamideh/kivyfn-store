# kivyfn/store/schema/fields/data/enumfield.py
from __future__ import annotations

from enum import Enum
from typing import Any, Iterable

from kivyfn.store.error import (
    FieldErrorDetail,
    FieldValidationError,
)
from kivyfn.store.schema.fields.data.datafield import DataField


class EnumField(DataField):
    """
    Strict enum field.

    Stored values are the underlying enum values rather than Enum instances.
    """

    def __init__(
        self,
        *,
        enum: type[Enum] | None = None,
        values: Iterable[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        if enum is not None and values is not None:
            raise ValueError(
                "EnumField accepts either enum or values, not both"
            )

        if enum is not None:
            self.values = tuple(
                item.value
                for item in enum
            )
        elif values is not None:
            self.values = tuple(values)
        else:
            raise ValueError(
                "EnumField requires enum or values"
            )

        if not self.values:
            raise ValueError(
                "EnumField requires at least one allowed value"
            )

    @property
    def metadata(self) -> dict[str, Any]:
        data = dict(super().metadata)

        data.update(
            {
                "values": self.values,
            }
        )

        return data

    def challenge(
        self,
        value: Any,
    ) -> None:
        super().challenge(value)

        if value is None:
            return

        if value not in self.values:
            raise FieldValidationError(
                "Value is not one of the allowed enum values",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="enum",
                    expected=list(self.values),
                ),
            )
