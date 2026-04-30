from .datafield import DataField
from ....errors import FieldError


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

        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise FieldError(
                field=self.name,
                message="Expected number",
                value=value,
                metadata=self.metadata,
            )

        if self.ge is not None and value < self.ge:
            raise FieldError(
                field=self.name,
                message=f"Must be >= {self.ge}",
                value=value,
                metadata=self.metadata,
            )

        if self.le is not None and value > self.le:
            raise FieldError(
                field=self.name,
                message=f"Must be <= {self.le}",
                value=value,
                metadata=self.metadata,
            )

