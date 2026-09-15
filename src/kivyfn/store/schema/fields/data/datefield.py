from __future__ import annotations

from datetime import date
from typing import Any

from kivyfn.store.error import FieldErrorDetail, FieldValidationError
from kivyfn.store.schema.fields.data.datafield import DataField


class DateField(DataField[str]):
    """ISO-8601 date with inclusive bounds and no value coercion."""

    def __init__(
        self, *, ge: str | None = None, le: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.ge = ge
        self.le = le
        self._minimum = None if ge is None else self._parse_iso(ge)
        self._maximum = None if le is None else self._parse_iso(le)
        if (
            self._minimum is not None
            and self._maximum is not None
            and self._minimum > self._maximum
        ):
            raise ValueError("ge must be less than or equal to le")

    @property
    def metadata(self) -> dict[str, Any]:
        return {**super().metadata, "ge": self.ge, "le": self.le}

    def challenge(self, value: Any) -> None:
        super().challenge(value)
        if value is None:
            return
        parsed = self._parse_iso(value)
        if self._minimum is not None and parsed < self._minimum:
            raise FieldValidationError(
                f"Date must be greater than or equal to {self.ge}",
                detail=FieldErrorDetail(
                    field=self.name, value=value, rule="ge", expected=self.ge
                ),
            )
        if self._maximum is not None and parsed > self._maximum:
            raise FieldValidationError(
                f"Date must be less than or equal to {self.le}",
                detail=FieldErrorDetail(
                    field=self.name, value=value, rule="le", expected=self.le
                ),
            )

    def _parse_iso(self, value: Any) -> date:
        if not isinstance(value, str):
            raise FieldValidationError(
                "Expected an ISO date string",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="type",
                    expected="ISO-8601 date string",
                ),
            )
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise FieldValidationError(
                "Invalid ISO date string",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="iso_date",
                    expected="YYYY-MM-DD",
                ),
            ) from exc
