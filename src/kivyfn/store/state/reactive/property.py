# src/kivyfn/store/state/reactive/property.py
from __future__ import annotations

from copy import deepcopy
from typing import Any

from kivy.properties import (
    BooleanProperty,
    NumericProperty,
    ObjectProperty,
    OptionProperty,
    Property,
    StringProperty,
)

from kivyfn.store.schema.fields import (
    BaseField,
    BooleanField,
    DateField,
    DatetimeField,
    EnumField,
    ForeignKeyField,
    NumericField,
    StringField,
    TimeField,
)


def state_property(field: BaseField[Any]) -> Property:
    """Build the Kivy Property for one reactive State field."""
    default = deepcopy(getattr(field, "default", None))

    if isinstance(field, BooleanField):
        return BooleanProperty(default, allownone=True)

    if isinstance(field, NumericField):
        return NumericProperty(default, allownone=True)

    if isinstance(field, EnumField):
        return OptionProperty(default, options=field.values, allownone=True)

    if isinstance(field, (StringField, DateField, DatetimeField, TimeField)):
        return StringProperty(default, allownone=True)

    if isinstance(field, ForeignKeyField):
        return StringProperty(None, allownone=True)

    return ObjectProperty(default, allownone=True, rebind=True)
