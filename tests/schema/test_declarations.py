from __future__ import annotations

from typing import Any

import pytest

from kivyfn.store.error import (
    FieldNotFoundError,
    FieldValidationError,
    InvalidOperationError,
)
from kivyfn.store.operations import OpCode
from kivyfn.store.schema import (
    ManySchema,
    NumericField,
    Signal,
    SingleSchema,
    StringField,
    trigger,
)


def test_schema_inheritance_uses_python_override_precedence() -> None:
    class ParentSchema(ManySchema):
        label = StringField(default="parent")
        amount = NumericField(default=1)
        ping = Signal[None]()
        indexes = [("label",)]
        uniques = [("amount",)]

        @trigger(OpCode.INSERT)
        def created(self, *_args: Any) -> str:
            return type(self).__name__

    class ChildSchema(ParentSchema):
        label = StringField(default="child")
        enabled = NumericField(default=2)

    child = ChildSchema()
    assert child.name == "ChildSchema"
    assert child.defaults() == {"label": "child", "amount": 1, "enabled": 2}
    assert child.field_names() == {"label", "amount", "enabled"}
    assert child.signal_names() == {"ping"}
    assert child.index_keys() == {("label",), ("amount",)}
    assert child.unique_keys() == {("amount",)}
    assert child.triggers()[0].fn() == "ChildSchema"
    assert ParentSchema().defaults() == {"label": "parent", "amount": 1}


def test_inherited_trigger_can_be_overridden_or_replaced() -> None:
    class Parent(ManySchema):
        count = NumericField(default=0)

        @trigger(OpCode.INSERT)
        def change(self, *_args: Any) -> str:
            return "parent"

    class NoTrigger(Parent):
        def change(self, *_args: Any) -> str:
            return "none"

    class ReplacedTrigger(Parent):
        @trigger(OpCode.REMOVE)
        def change(self, *_args: Any) -> str:
            return "child"

    assert NoTrigger().triggers() == []
    only = ReplacedTrigger().triggers()
    assert len(only) == 1
    assert only[0].opcode is OpCode.REMOVE
    assert only[0].fn() == "child"


def test_schema_collections_are_detached_and_metadata_is_read_only() -> None:
    class Sample(SingleSchema):
        age = NumericField(default=1)
        ping = Signal[dict[str, Any]]()

    schema = Sample()
    assert schema.get_field("age") is Sample.age
    assert schema.get_signal("ping") is Sample.ping
    assert schema.has_signal("ping")
    schema.fields().clear()
    schema.signals().clear()
    schema.field_names().clear()
    schema.signal_names().clear()
    assert schema.field_names() == {"age"}
    assert schema.signal_names() == {"ping"}
    assert "ping" in repr(Sample.ping)
    with pytest.raises(AttributeError):
        schema.age = 10
    with pytest.raises(AttributeError):
        schema.ping = None  # type: ignore[assignment]
    with pytest.raises(FieldNotFoundError):
        schema.get_field("missing")
    with pytest.raises(InvalidOperationError):
        schema.get_signal("missing")


def test_field_failure_enrichment_preserves_specific_detail() -> None:
    class Limited(SingleSchema):
        count = NumericField(default=1, ge=0)

    with pytest.raises(FieldValidationError) as caught:
        Limited().challenge_fields({"count": -1})
    detail: Any = caught.value.detail
    assert detail.schema == "Limited"
    assert detail.field == "count"
    assert detail.rule == "ge"
    assert detail.minimum == 0


@pytest.mark.parametrize(
    "name", ["name", "fields", "get_field", "_build_fields", "_fields"]
)
@pytest.mark.parametrize("metadata", [NumericField, Signal])
def test_schema_metadata_cannot_shadow_its_own_runtime_api(
    name: str, metadata: Any
) -> None:
    schema_type = type("BadDeclaration", (SingleSchema,), {name: metadata()})
    with pytest.raises(ValueError, match="reserved"):
        schema_type()


@pytest.mark.parametrize("declaration", [NumericField(), Signal()])
def test_one_descriptor_cannot_be_declared_under_two_names(
    declaration: Any,
) -> None:
    with pytest.raises((ValueError, RuntimeError)) as caught:
        type(
            "DuplicateDescriptor",
            (SingleSchema,),
            {"first": declaration, "second": declaration},
        )
    cause = caught.value.__cause__ or caught.value
    assert isinstance(cause, ValueError)
    assert "multiple names" in str(cause)


@pytest.mark.parametrize("kind", ["indexes", "uniques"])
@pytest.mark.parametrize("key", [(), "abc", ("missing",), ("a", "a"), (1,)])
def test_index_declarations_reject_malformed_or_unknown_fields(
    kind: str, key: Any
) -> None:
    schema_type = type(
        "BadIndex", (ManySchema,), {"a": NumericField(default=0), kind: [key]}
    )
    with pytest.raises(ValueError):
        schema_type()


def test_index_metadata_deduplicates_declarations_without_aliasing() -> None:
    class Indexed(ManySchema):
        first = NumericField(default=0)
        second = NumericField(default=0)
        indexes = [("first",), ("first",)]
        uniques = [("first", "second")]

    schema = Indexed()
    schema.index_keys().clear()
    schema.unique_keys().clear()
    assert schema.index_keys() == {("first",), ("first", "second")}
    assert schema.unique_keys() == {("first", "second")}


def test_stacked_triggers_match_specific_fields_and_preserve_binding() -> None:
    class Triggered(ManySchema):
        amount = NumericField(default=0)

        @trigger(OpCode.SETROW, field="amount")
        @trigger(OpCode.INSERT)
        def updated(self, *_args: Any) -> str:
            return self.name

    triggers = Triggered().triggers()
    assert len(triggers) == 2
    assert all(item.fn() == "Triggered" for item in triggers)
    assert triggers[0].matches(OpCode.INSERT, None)
    assert triggers[1].matches(OpCode.SETROW, "amount")
    assert not triggers[1].matches(OpCode.SETROW, "other")
    assert not triggers[1].matches(OpCode.SET, "amount")


@pytest.mark.parametrize("opcode", [OpCode.INSERT, OpCode.SETROW])
def test_trigger_rejects_unknown_or_inapplicable_field(opcode: OpCode) -> None:
    class InvalidTrigger(ManySchema):
        @trigger(opcode, field="missing")
        def invalid(self, *_args: Any) -> None:
            pass

    with pytest.raises(ValueError):
        InvalidTrigger()


def test_trigger_decorator_rejects_invalid_declaration() -> None:
    with pytest.raises(ValueError):
        trigger("invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        trigger(OpCode.SET, field="")
