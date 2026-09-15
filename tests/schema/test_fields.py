from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any

import pytest

from kivyfn.store.error import FieldValidationError
from kivyfn.store.schema import (
    BaseField,
    BooleanField,
    DataField,
    DateField,
    DatetimeField,
    EnumField,
    ForeignKeyField,
    ForeignKeyPolicy,
    NumericField,
    SingleSchema,
    StringField,
    TimeField,
)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (BooleanField(), True),
        (BooleanField(), False),
        (StringField(min_length=1, max_length=3), "a"),
        (StringField(min_length=1, max_length=3), "abc"),
        (StringField(min_length=1, max_length=1), "é"),
        (NumericField(ge=0, le=1), 0),
        (NumericField(ge=0, le=1), 1),
        (NumericField(ge=0, le=1), 0.5),
        (NumericField(), 10**500),
        (DateField(ge="2024-02-29", le="2024-03-01"), "2024-02-29"),
        (DateField(ge="2024-02-29", le="2024-03-01"), "2024-03-01"),
        (TimeField(ge="01:00", le="02:00"), "01:30:00.001"),
        (TimeField(ge="12:00Z", le="13:00Z"), "14:30+02:00"),
        (DatetimeField(ge="2026-01-01T12:00Z"), "2026-01-01T14:00+02:00"),
        (TimeField(), "02:00Z"),
        (TimeField(), "02:00"),
        (DatetimeField(), "2026-01-01T02:00Z"),
        (DatetimeField(), "2026-01-01T02:00"),
        (EnumField(values=("one", "two")), "one"),
        (EnumField(values=(1, 2)), 1),
        (ForeignKeyField("Target"), None),
        (ForeignKeyField("Target"), "row"),
        (DataField(), {"nested": [1, True, None]}),
    ],
)
def test_field_accepts_valid_value_without_coercion(
    field: BaseField[Any], value: Any
) -> None:
    assert field.challenge(value) is None


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        (BooleanField(), 1, "type"),
        (BooleanField(), "true", "type"),
        (StringField(), 42, "type"),
        (StringField(), b"value", "type"),
        (StringField(min_length=2), "a", "min_length"),
        (StringField(max_length=2), "abc", "max_length"),
        (NumericField(), True, "type"),
        (NumericField(), "1", "type"),
        (NumericField(ge=0), -1, "ge"),
        (NumericField(le=1), 2, "le"),
        (NumericField(), float("nan"), "finite"),
        (NumericField(ge=0, le=10), float("nan"), "finite"),
        (NumericField(), float("inf"), "finite"),
        (NumericField(), float("-inf"), "finite"),
        (DateField(), "2023-02-29", "iso_date"),
        (DateField(), "2024-01-01T12:00", "iso_date"),
        (DateField(), 20240101, "type"),
        (DateField(ge="2024-01-02"), "2024-01-01", "ge"),
        (DateField(le="2024-01-01"), "2024-01-02", "le"),
        (TimeField(), "25:00", "iso_time"),
        (TimeField(), 1200, "type"),
        (TimeField(ge="01:00"), "00:59:59", "ge"),
        (TimeField(le="01:00"), "01:00:00.000001", "le"),
        (DatetimeField(), "invalid", "iso_datetime"),
        (DatetimeField(), 2024, "type"),
        (DatetimeField(ge="2024-01-02T00:00"), "2024-01-01T23:59:59", "ge"),
        (
            DatetimeField(le="2024-01-02T00:00"),
            "2024-01-02T00:00:00.000001",
            "le",
        ),
        (EnumField(values=(1, 2)), True, "enum"),
        (EnumField(values=(1, 2)), 1.0, "enum"),
        (EnumField(values=("one", "two")), "three", "enum"),
        (ForeignKeyField("Target"), "", "type"),
        (ForeignKeyField("Target"), 1, "type"),
        (ForeignKeyField("Target"), [], "type"),
    ],
)
def test_field_failure_identifies_its_rule(
    field: BaseField[Any], value: Any, rule: str
) -> None:
    with pytest.raises(FieldValidationError) as caught:
        field.challenge(value)
    assert caught.value.detail is not None
    assert getattr(caught.value.detail, "rule") == rule


@pytest.mark.parametrize(
    "field_type",
    [
        BooleanField,
        StringField,
        NumericField,
        DateField,
        TimeField,
        DatetimeField,
        DataField,
    ],
)
def test_none_is_controlled_by_allow_none(field_type: Any) -> None:
    field_type(allow_none=True).challenge(None)
    with pytest.raises(FieldValidationError, match="cannot be None"):
        field_type(allow_none=False).challenge(None)


def test_enum_none_policy_and_underlying_enum_values() -> None:
    class Choice(Enum):
        A = "a"
        B = "b"

    class Number(IntEnum):
        ONE = 1

    field = EnumField(enum=Choice, allow_none=True)
    field.challenge(None)
    field.challenge("a")
    with pytest.raises(FieldValidationError):
        field.challenge(Choice.A)  # type: ignore[arg-type]
    with pytest.raises(FieldValidationError):
        EnumField(enum=Number).challenge(Number.ONE)
    with pytest.raises(FieldValidationError):
        EnumField(values=(None, 1), allow_none=False).challenge(None)


@pytest.mark.parametrize("field_type", [TimeField, DatetimeField])
@pytest.mark.parametrize("bound_name", ["ge", "le"])
@pytest.mark.parametrize("aware_bound", [False, True])
def test_timezone_awareness_mismatch_is_an_expected_field_error(
    field_type: Any, bound_name: str, aware_bound: bool
) -> None:
    prefix = "2026-01-01T" if field_type is DatetimeField else ""
    bound = prefix + "12:00" + ("Z" if aware_bound else "")
    value = prefix + "12:00" + ("" if aware_bound else "+00:00")
    field = field_type(**{bound_name: bound})
    with pytest.raises(FieldValidationError) as caught:
        field.challenge(value)
    assert getattr(caught.value.detail, "rule") == "timezone_awareness"


@pytest.mark.parametrize(
    ("field_type", "options"),
    [
        (NumericField, {"ge": True}),
        (NumericField, {"le": "1"}),
        (NumericField, {"ge": float("nan")}),
        (NumericField, {"le": float("inf")}),
        (NumericField, {"ge": 2, "le": 1}),
        (StringField, {"min_length": -1}),
        (StringField, {"max_length": -1}),
        (StringField, {"min_length": 1.5}),
        (StringField, {"max_length": True}),
        (StringField, {"min_length": 2, "max_length": 1}),
        (DateField, {"ge": "2024-01-02", "le": "2024-01-01"}),
        (TimeField, {"ge": "12:01", "le": "12:00"}),
        (TimeField, {"ge": "12:00Z", "le": "12:01"}),
        (DatetimeField, {"ge": "2026-01-02", "le": "2026-01-01"}),
        (DatetimeField, {"ge": "2026-01-01T12:00Z", "le": "2026-01-01T12:01"}),
        (EnumField, {}),
        (EnumField, {"values": ()}),
    ],
)
def test_invalid_field_configuration_fails_at_declaration(
    field_type: Any, options: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        field_type(**options)


@pytest.mark.parametrize("field_type", [DateField, TimeField, DatetimeField])
@pytest.mark.parametrize("bound", ["invalid", 42])
def test_malformed_temporal_bounds_are_field_errors(
    field_type: Any, bound: Any
) -> None:
    with pytest.raises(FieldValidationError):
        field_type(ge=bound)


def test_defaults_and_metadata_do_not_share_mutable_values() -> None:
    original = {"values": [1]}

    class MutableSchema(SingleSchema):
        value = DataField(default=original)

    original["values"].append(2)
    first = MutableSchema().defaults()
    second = MutableSchema().defaults()
    first["value"]["values"].append(3)
    metadata = MutableSchema.value.metadata
    metadata["default"]["values"].append(4)
    assert second == {"value": {"values": [1]}}
    assert MutableSchema().defaults() == second
    assert MutableSchema.value.metadata["default"] == {"values": [1]}


def test_enum_metadata_and_source_values_are_detached() -> None:
    source: list[dict[str, Any]] = [{"nested": [1]}]
    field = EnumField(values=source)
    source[0]["nested"].append(2)
    metadata: Any = field.metadata
    metadata["values"][0]["nested"].append(3)
    field.challenge({"nested": [1]})
    assert field.values == ({"nested": [1]},)


def test_foreign_key_relation_names_and_delete_policy() -> None:
    class RelationSchema(SingleSchema):
        parent_id = ForeignKeyField("Parent")
        boss = ForeignKeyField("Parent", relation="manager")

    schema = RelationSchema()
    assert RelationSchema.parent_id.relation_name() == "parent"
    assert RelationSchema.boss.relation_name() == "manager"
    assert ForeignKeyField("Parent").relation_name("owner") == "owner"
    assert schema.index_keys() == {("parent_id",), ("boss",)}
    assert RelationSchema.parent_id.on_delete is ForeignKeyPolicy.RESTRICT


@pytest.mark.parametrize(
    "options",
    [
        {"schema": ""},
        {"schema": 1},
        {"schema": "Parent", "relation": ""},
        {"schema": "Parent", "relation": "a.b"},
        {"schema": "Parent", "relation": 1},
        {"schema": "Parent", "on_delete": "invalid"},
    ],
)
def test_invalid_foreign_key_declaration(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ForeignKeyField(**options)
