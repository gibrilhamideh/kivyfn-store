# src/kivyfn/store/schema/fields/data/booleanfield.py
from __future__ import annotations

from typing import Any

from kivyfn.store.error import (
    FieldErrorDetail,
    FieldValidationError,
)
from kivyfn.store.schema.fields.data.datafield import DataField


class BooleanField(DataField[bool]):
    """
    Strict boolean field.

    Accepts bool only. No coercion or normalization is performed.
    """

    def challenge(
        self,
        value: Any,
    ) -> None:
        super().challenge(value)

        if value is None:
            return

        if not isinstance(value, bool):
            raise FieldValidationError(
                "Expected a boolean value",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="type",
                    expected="bool",
                ),
            )
