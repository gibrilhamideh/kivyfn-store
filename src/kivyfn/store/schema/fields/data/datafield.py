from __future__ import annotations
from typing import Any
from ..base import BaseField
from ....errors import FieldError


class DataField(BaseField):
    """
    Base class for all schema fields.

    Fields are DESCRIPTORS on Schema classes.
    They define structure and validation rules only.
    """

    def __init__(
        self,
        *,
        default=None,
        allow_none=False,
    ):
        super().__init__()
        self.default: Any = default
        self.allow_none: bool = allow_none

    @property
    def metadata(self):
        return {
            "default": self.default,
            "allow_none": self.allow_none,
        }

    def challenge(self, value: Any) -> None:
        if value is None and not self.allow_none:
            raise FieldError(self.name, "value not allowed to be None")
