from kivyfn.store.schema.fields.data.datafield import DataField
from kivyfn.store.errors.store import FieldValidationError


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
            raise FieldValidationError(
                "Expected boolean",
                field=self.name,
                value=value,
            )
