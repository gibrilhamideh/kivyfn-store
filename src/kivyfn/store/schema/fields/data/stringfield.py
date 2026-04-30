from .datafield import DataField
from ....errors import FieldError


class StringField(DataField):
    """
    Strict string field.

    Accepts str only.
    No normalization or coercion.
    """

    def __init__(
        self,
        *,
        min_length: int | None = None,
        max_length: int | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.min_length = min_length
        self.max_length = max_length

        if min_length is not None and min_length < 0:
            raise ValueError("min_length must be >= 0")

        if max_length is not None and max_length < 0:
            raise ValueError("max_length must be >= 0")

        if (
            min_length is not None
            and max_length is not None
            and min_length > max_length
        ):
            raise ValueError("min_length must be <= max_length")

    @property
    def metadata(self) -> dict:
        data = dict(super().metadata)
        data.update(
            {
                "min_length": self.min_length,
                "max_length": self.max_length,
            }
        )
        return data

    def challenge(self, value) -> None:
        super().challenge(value)

        if value is None:
            return

        if not isinstance(value, str):
            raise FieldError(
                field=self.name,
                message="Expected string",
                value=value,
                metadata=self.metadata,
            )

        length = len(value)

        if self.min_length is not None and length < self.min_length:
            raise FieldError(
                field=self.name,
                message=f"Length must be >= {self.min_length}",
                value=value,
                metadata=self.metadata,
            )

        if self.max_length is not None and length > self.max_length:
            raise FieldError(
                field=self.name,
                message=f"Length must be <= {self.max_length}",
                value=value,
                metadata=self.metadata,
            )

