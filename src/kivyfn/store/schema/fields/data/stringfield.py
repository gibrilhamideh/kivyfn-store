# kivyfn/store/schema/fields/data/stringfield.py
from __future__ import annotations

from typing import Any

from kivyfn.store.error import (
    FieldErrorDetail,
    FieldValidationError,
)
from kivyfn.store.schema.fields.data.datafield import DataField


class StringField(DataField[str]):
    """
    Strict string field.

    Accepts str values only. No normalization or coercion is performed.
    """

    def __init__(
        self,
        *,
        min_length: int | None = None,
        max_length: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        if min_length is not None and min_length < 0:
            raise ValueError(
                "min_length must be greater than or equal to 0"
            )

        if max_length is not None and max_length < 0:
            raise ValueError(
                "max_length must be greater than or equal to 0"
            )

        if (
            min_length is not None
            and max_length is not None
            and min_length > max_length
        ):
            raise ValueError(
                "min_length must be less than or equal to max_length"
            )

        self.min_length = min_length
        self.max_length = max_length

    @property
    def metadata(self) -> dict[str, Any]:
        data = dict(super().metadata)

        data.update(
            {
                "min_length": self.min_length,
                "max_length": self.max_length,
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

        if not isinstance(value, str):
            raise FieldValidationError(
                "Expected a string value",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="type",
                    expected="str",
                ),
            )

        length = len(value)

        if (
            self.min_length is not None
            and length < self.min_length
        ):
            raise FieldValidationError(
                (
                    "String length must be greater than or equal to "
                    f"{self.min_length}"
                ),
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="min_length",
                    minimum=self.min_length,
                ),
            )

        if (
            self.max_length is not None
            and length > self.max_length
        ):
            raise FieldValidationError(
                (
                    "String length must be less than or equal to "
                    f"{self.max_length}"
                ),
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="max_length",
                    maximum=self.max_length,
                ),
            )
