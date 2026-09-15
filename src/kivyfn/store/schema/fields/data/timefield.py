from __future__ import annotations

from datetime import time
from typing import Any

from kivyfn.store.error import FieldErrorDetail, FieldValidationError
from kivyfn.store.schema.fields.data.datafield import DataField


class TimeField(DataField[str]):
    """ISO-8601 time with inclusive bounds and no value coercion.

    Bounded values must use the same timezone awareness as their bounds.
    """

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
            and (self._minimum.utcoffset() is None)
            != (self._maximum.utcoffset() is None)
        ):
            raise ValueError("Temporal bounds must have matching awareness")
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
        bound = self._minimum or self._maximum
        if bound is not None and (
            (parsed.utcoffset() is None) != (bound.utcoffset() is None)
        ):
            raise FieldValidationError(
                "Value and bounds must have matching timezone awareness",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="timezone_awareness",
                    expected=(
                        "naive" if bound.utcoffset() is None else "aware"
                    ),
                ),
            )

        if self._minimum is not None and parsed < self._minimum:
            raise FieldValidationError(
                f"Time must be greater than or equal to {self.ge}",
                detail=FieldErrorDetail(
                    field=self.name, value=value, rule="ge", expected=self.ge
                ),
            )
        if self._maximum is not None and parsed > self._maximum:
            raise FieldValidationError(
                f"Time must be less than or equal to {self.le}",
                detail=FieldErrorDetail(
                    field=self.name, value=value, rule="le", expected=self.le
                ),
            )

    def _parse_iso(self, value: Any) -> time:
        if not isinstance(value, str):
            raise FieldValidationError(
                "Expected an ISO time string",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="type",
                    expected="ISO-8601 time string",
                ),
            )
        try:
            return time.fromisoformat(value)
        except ValueError as exc:
            raise FieldValidationError(
                "Invalid ISO time string",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="iso_time",
                    expected="HH:MM[:SS[.ffffff]]",
                ),
            ) from exc
