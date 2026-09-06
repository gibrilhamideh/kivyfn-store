# kivyfn/store/schema/fields/data/datetimefield.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from kivyfn.store.error import (
    FieldErrorDetail,
    FieldValidationError,
)
from kivyfn.store.schema.fields.data.datafield import DataField


class DatetimeField(DataField[str]):
    """
    ISO-8601 datetime field with optional inclusive lower and upper bounds.
    """

    def __init__(
        self,
        *,
        ge: str | None = None,
        le: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self.ge = ge
        self.le = le

        if ge is not None:
            self._parse_iso(
                ge,
                label="ge",
            )

        if le is not None:
            self._parse_iso(
                le,
                label="le",
            )

    @property
    def metadata(self) -> dict[str, Any]:
        data = dict(super().metadata)

        data.update(
            {
                "ge": self.ge,
                "le": self.le,
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
                "Expected an ISO datetime string",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="type",
                    expected="ISO-8601 datetime string",
                ),
            )

        parsed = self._parse_iso(
            value,
            label="value",
        )

        if self.ge is not None:
            minimum = self._parse_iso(
                self.ge,
                label="ge",
            )

            if parsed < minimum:
                raise FieldValidationError(
                    (
                        "Datetime must be greater than or equal to "
                        f"{self.ge}"
                    ),
                    detail=FieldErrorDetail(
                        field=self.name,
                        value=value,
                        rule="ge",
                        expected=self.ge,
                    ),
                )

        if self.le is not None:
            maximum = self._parse_iso(
                self.le,
                label="le",
            )

            if parsed > maximum:
                raise FieldValidationError(
                    (
                        "Datetime must be less than or equal to "
                        f"{self.le}"
                    ),
                    detail=FieldErrorDetail(
                        field=self.name,
                        value=value,
                        rule="le",
                        expected=self.le,
                    ),
                )

    def _parse_iso(
        self,
        value: str,
        *,
        label: str,
    ) -> datetime:
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise FieldValidationError(
                f"Invalid ISO datetime string for {label}",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="iso_datetime",
                    expected="ISO-8601 datetime string",
                ),
            ) from exc
