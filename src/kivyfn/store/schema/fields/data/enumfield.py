# kivyfn/store/schema/fields/data/enumfield.py
from __future__ import annotations

from copy import deepcopy
from enum import Enum
from typing import Generic, Iterable, TypeVar, cast

from kivyfn.store.error import (
    FieldErrorDetail,
    FieldValidationError,
    ProtocolValue,
)
from kivyfn.store.schema.fields.data.datafield import DataField

ValueT = TypeVar("ValueT", bound=ProtocolValue)


class EnumField(DataField[ValueT], Generic[ValueT]):
    """
    Strict enum field.

    Stored values are the underlying enum values rather than Enum instances.
    """

    def __init__(
        self,
        *,
        enum: type[Enum] | None = None,
        values: Iterable[ValueT] | None = None,
        default: ValueT | None = None,
        allow_none: bool = False,
    ) -> None:
        super().__init__(default=default, allow_none=allow_none)

        if enum is not None and values is not None:
            raise ValueError(
                "EnumField accepts either enum or values, not both"
            )

        self.values: tuple[ValueT, ...]

        if enum is not None:
            self.values = deepcopy(
                tuple(cast(ValueT, item.value) for item in enum)
            )
        elif values is not None:
            self.values = deepcopy(tuple(values))
        else:
            raise ValueError("EnumField requires enum or values")

        if not self.values:
            raise ValueError("EnumField requires at least one allowed value")

    @property
    def metadata(self) -> dict[str, ProtocolValue]:
        return {**super().metadata, "values": deepcopy(list(self.values))}

    def challenge(self, value: ProtocolValue) -> None:
        super().challenge(value)

        if value is None:
            return

        if not any(
            type(value) is type(item) and value == item for item in self.values
        ):
            expected: list[ProtocolValue] = [item for item in self.values]

            raise FieldValidationError(
                "Value is not one of the allowed enum values",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="enum",
                    expected=expected,
                ),
            )
