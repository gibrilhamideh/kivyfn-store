from .datafield import DataField
from ....errors import FieldError


class BooleanField(DataField):
    """
    Strict boolean field.
    Accepts bool only.
    No coercion, no normalization.
    """

    def challenge(self, value):
        super().challenge(value)

        if value is None:
            return  # already validated by allow_none

        if not isinstance(value, bool):
            raise FieldError(
                field=self.name,
                message="Expected boolean",
                value=value,
                metadata=self.metadata,
            )

