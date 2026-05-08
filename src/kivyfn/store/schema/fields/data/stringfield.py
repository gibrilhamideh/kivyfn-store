from kivyfn.store.schema.fields.data.datafield import DataField
from kivyfn.store.errors.store import FieldValidationError


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
            raise FieldValidationError(
                "Expected string",
                field=self.name,
                value=value,
                metadata=self.metadata,
            )

        length = len(value)

        if self.min_length is not None and length < self.min_length:
            raise FieldValidationError(
                f"Length must be >= {self.min_length}",
                field=self.name,
                value=value,
                metadata=self.metadata,
            )

        if self.max_length is not None and length > self.max_length:
            raise FieldValidationError(
                f"Length must be <= {self.max_length}",
                field=self.name,
                value=value,
                metadata=self.metadata,
            )
