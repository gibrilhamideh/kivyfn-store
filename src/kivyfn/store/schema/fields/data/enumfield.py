from enum import Enum

from .datafield import DataField
from ....errors import FieldError


class EnumField(DataField):
    """
    Strict enum field.

    Store value:
        string/int/etc. from the enum value, not the Enum object.
    """

    def __init__(self, *, enum: type[Enum] | None = None, values=None, **kwargs):
        super().__init__(**kwargs)

        if enum is not None:
            self.values = tuple(item.value for item in enum)
        elif values is not None:
            self.values = tuple(values)
        else:
            raise ValueError("EnumField requires enum or values")

    @property
    def metadata(self) -> dict:
        data = dict(super().metadata)
        data.update({"values": self.values})
        return data

    def challenge(self, value) -> None:
        super().challenge(value)

        if value is None:
            return

        if value not in self.values:
            raise FieldError(
                field=self.name,
                message="Invalid enum value",
                value=value,
                metadata=self.metadata,
            )

