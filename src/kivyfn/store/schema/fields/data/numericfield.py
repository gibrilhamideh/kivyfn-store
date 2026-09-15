# kivyfn/store/schema/fields/data/numericfield.py
from __future__ import annotations

from math import isfinite
from typing import Any

from kivyfn.store.error import FieldErrorDetail, FieldValidationError
from kivyfn.store.schema.fields.data.datafield import DataField


class NumericField(DataField[int | float]):
    """
    Strict numeric field.

    Accepts int and float values only. Boolean values are rejected because
    bool is a subclass of int. Non-finite floats are rejected.
    """

    def __init__(
        self,
        *,
        ge: int | float | None = None,
        le: int | float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        for label, bound in (("ge", ge), ("le", le)):
            if bound is None:
                continue
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                raise ValueError(f"{label} must be a finite number")
            if isinstance(bound, float) and not isfinite(bound):
                raise ValueError(f"{label} must be a finite number")
        if ge is not None and le is not None and ge > le:
            raise ValueError("ge must be less than or equal to le")

        self.ge = ge
        self.le = le

    @property
    def metadata(self) -> dict[str, Any]:
        data = dict(super().metadata)

        data.update({"ge": self.ge, "le": self.le})

        return data

    def challenge(self, value: Any) -> None:
        super().challenge(value)

        if value is None:
            return

        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise FieldValidationError(
                "Expected a numeric value",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="type",
                    expected="int or float",
                ),
            )

        if isinstance(value, float) and not isfinite(value):
            raise FieldValidationError(
                "Expected a finite numeric value",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=str(value),
                    rule="finite",
                    expected="finite int or float",
                ),
            )

        if self.ge is not None and value < self.ge:
            raise FieldValidationError(
                f"Value must be greater than or equal to {self.ge}",
                detail=FieldErrorDetail(
                    field=self.name, value=value, rule="ge", minimum=self.ge
                ),
            )

        if self.le is not None and value > self.le:
            raise FieldValidationError(
                f"Value must be less than or equal to {self.le}",
                detail=FieldErrorDetail(
                    field=self.name, value=value, rule="le", maximum=self.le
                ),
            )
