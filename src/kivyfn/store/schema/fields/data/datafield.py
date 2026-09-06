# src/kivyfn/store/schema/fields/data/datafield.py
from __future__ import annotations

from typing import Any, Generic, TypeVar

from kivyfn.store.error import (
    FieldErrorDetail,
    FieldValidationError,
)
from kivyfn.store.schema.fields.base import BaseField


ValueT = TypeVar("ValueT")


class DataField(BaseField[ValueT], Generic[ValueT]):
    """
    Base class for schema data fields.

    Fields are descriptors declared on Schema classes. They define data
    structure and validation rules only.
    """

    def __init__(
        self,
        *,
        default: ValueT | None = None,
        allow_none: bool = False,
    ) -> None:
        super().__init__()

        self.default = default
        self.allow_none = allow_none

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "default": self.default,
            "allow_none": self.allow_none,
        }

    def challenge(
        self,
        value: Any,
    ) -> None:
        if value is None and not self.allow_none:
            raise FieldValidationError(
                "Value cannot be None",
                detail=FieldErrorDetail(
                    field=self.name,
                    value=value,
                    rule="allow_none",
                    allow_none=False,
                ),
            )
