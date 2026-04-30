from datetime import datetime
from .datafield import DataField
from ....errors import FieldError


class DatetimeField(DataField):
    """
    Strict datetime field.

    Store value:
        ISO 8601 string

    Example:
        "2026-04-27T14:30:00+03:00"

    No normalization.
    No coercion.
    """

    def __init__(
        self,
        *,
        ge: str | None = None,
        le: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.ge = ge
        self.le = le

        if ge is not None:
            self._parse_iso(ge, "ge")

        if le is not None:
            self._parse_iso(le, "le")

    @property
    def metadata(self) -> dict:
        data = dict(super().metadata)
        data.update({"ge": self.ge, "le": self.le})
        return data

    def challenge(self, value) -> None:
        super().challenge(value)

        if value is None:
            return

        if not isinstance(value, str):
            raise FieldError(
                field=self.name,
                message="Expected ISO datetime string",
                value=value,
                metadata=self.metadata,
            )

        parsed = self._parse_iso(value, "value")

        if self.ge is not None:
            ge = self._parse_iso(self.ge, "ge")
            if parsed < ge:
                raise FieldError(
                    field=self.name,
                    message=f"Must be >= {self.ge}",
                    value=value,
                    metadata=self.metadata,
                )

        if self.le is not None:
            le = self._parse_iso(self.le, "le")
            if parsed > le:
                raise FieldError(
                    field=self.name,
                    message=f"Must be <= {self.le}",
                    value=value,
                    metadata=self.metadata,
                )

    def _parse_iso(self, value: str, label: str) -> datetime:
        try:
            return datetime.fromisoformat(value)
        except ValueError as e:
            raise FieldError(
                field=self.name,
                message=f"Invalid ISO datetime string for {label}",
                value=value,
                metadata=self.metadata,
            ) from e

