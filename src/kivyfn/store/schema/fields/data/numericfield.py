from kivyfn.store.schema.fields.data.datafield import DataField
from kivyfn.store.errors.store import FieldValidationError


class NumericField(DataField):
    """
    Strict numeric field.

    Accepts int/float only.
    No coercion.
    """

    def __init__(
        self,
        *,
        ge: int | float | None = None,
        le: int | float | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.ge = ge
        self.le = le

    @property
    def metadata(self) -> dict:
        data = dict(super().metadata)
        data.update({"ge": self.ge, "le": self.le})
        return data

    def challenge(self, value) -> None:
        super().challenge(value)

        if value is None:
            return

        # Reject bool explicitly (bool is subclass of int)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise FieldValidationError(
                "Expected number",
                field=self.name,
                value=value,
                metadata=self.metadata,
            )

        if self.ge is not None and value < self.ge:
            raise FieldValidationError(
                f"Must be >= {self.ge}",
                field=self.name,
                value=value,
                metadata=self.metadata,
            )

        if self.le is not None and value > self.le:
            raise FieldValidationError(
                f"Must be <= {self.le}",
                field=self.name,
                value=value,
                metadata=self.metadata,
            )
