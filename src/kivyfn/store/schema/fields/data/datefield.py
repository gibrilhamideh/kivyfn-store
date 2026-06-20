from datetime import date

from kivyfn.store.errors.store import FieldValidationError
from kivyfn.store.schema.fields.data.datafield import DataField


class DateField(DataField):
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
            raise FieldValidationError(
                "Expected ISO date string",
                field=self.name,
                value=value,
                metadata=self.metadata,
            )

        parsed = self._parse_iso(value, "value")

        if self.ge is not None:
            ge = self._parse_iso(self.ge, "ge")
            if parsed < ge:
                raise FieldValidationError(
                    f"Must be >= {self.ge}",
                    field=self.name,
                    value=value,
                    metadata=self.metadata,
                )

        if self.le is not None:
            le = self._parse_iso(self.le, "le")
            if parsed > le:
                raise FieldValidationError(
                    f"Must be <= {self.le}",
                    field=self.name,
                    value=value,
                    metadata=self.metadata,
                )

    def _parse_iso(self, value: str, label: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise FieldValidationError(
                f"Invalid ISO date string for {label}",
                field=self.name,
                value=value,
                metadata=self.metadata,
            ) from exc
