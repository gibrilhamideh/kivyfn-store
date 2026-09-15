from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.error import InvalidOperationError
from kivyfn.store.operations import OpCode
from kivyfn.store.protocol.message.requests import RemoveRequestAction
from kivyfn.store.schema import (
    DataField,
    ForeignKeyField,
    ManySchema,
    NumericField,
    SingleSchema,
)
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.trigger import trigger
from kivyfn.store.transaction import Transaction
from tests.core.helpers import insert, make_store, setrow, setvalue


class Parents(ManySchema):
    number = NumericField(default=0)
    payload = DataField[dict[str, Any]](default={"items": []})


def test_single_context_boundaries_metadata_and_values_are_detached(
    tmp_path: Path,
) -> None:
    observed = []

    class Root(SingleSchema):
        number = NumericField(default=0)
        payload = DataField[dict[str, Any]](default={"items": []})

        @trigger(OpCode.SET, field="payload")
        def inspect(self, tx: Transaction, ctx: TriggerContext) -> None:
            before = ctx.single(ctx.node, before=ctx.operation)
            through = ctx.single(ctx.node, through=ctx.operations[0])
            after = ctx.single(ctx.node, through=ctx.operation)
            observed.append((before, through, after))
            assert ctx.opcode is OpCode.SET
            assert ctx.id is None
            assert ctx.key == "payload"
            assert ctx.instruction is ctx.operation.instruction
            assert ctx.instruction.opcode is OpCode.SET
            payload = ctx.value
            payload["items"].append("value copy")
            after["payload"]["items"].append("single copy")
            assert ctx.current()["payload"] == {"items": [3]}

    store = make_store(tmp_path, Parents, root_schema=Root)
    result = store.execute(
        (
            setvalue(Root, number=1),
            insert(Parents, "other", number=2),
            setvalue(Root, payload={"items": [3]}),
        ),
        transaction_id="boundaries",
    )
    assert result.success
    before, through, _ = observed[0]
    assert before == through == {"number": 1, "payload": {"items": []}}
    assert store.snapshot_by_schema("Root") == {
        "number": 1,
        "payload": {"items": [3]},
    }


def test_foreign_reference_sees_prior_logical_operations_and_owns_row(
    tmp_path: Path,
) -> None:
    observed = []

    class Children(ManySchema):
        parent_id = ForeignKeyField("Parents")
        payload = DataField[dict[str, Any]](default={"items": []})

        @trigger(OpCode.INSERT)
        def inspect(self, tx: Transaction, ctx: TriggerContext) -> None:
            reference = ctx.require_foreignkey("parent_id")
            assert reference.id == "first"
            assert reference.node.schema.name == "Parents"
            assert reference.row["number"] == 3
            reference.row["payload"]["items"].append("detached")
            assert ctx.row(reference.node, "first") == {
                "number": 3,
                "payload": {"items": []},
            }
            assert ctx.row(reference.node, "second") is None
            original = ctx.row(
                reference.node, "first", through=ctx.operations[0]
            )
            assert original == {"number": 1, "payload": {"items": []}}
            assert ctx.key is None
            assert ctx.id == "child"
            ctx.value["payload"]["items"].append("insert copy")
            assert ctx.current()["payload"] == {"items": []}
            observed.append(reference.id)

    store = make_store(tmp_path, Parents, Children)
    result = store.execute(
        (
            insert(Parents, "first", number=1),
            insert(Parents, "second", number=2),
            setrow(Parents, "first", number=3),
            RemoveRequestAction("Parents", "second"),
            insert(Children, "child", parent_id="first"),
        ),
        transaction_id="logical-reference",
    )
    assert result.success
    assert observed == ["first"]
    assert store.snapshot_by_schema("Parents") == {
        "first": {"number": 3, "payload": {"items": []}}
    }


@pytest.mark.parametrize("required", [False, True])
def test_optional_and_required_foreign_key_contexts_preserve_atomicity(
    tmp_path: Path, required: bool
) -> None:
    observed = []

    class Root(SingleSchema):
        number = NumericField(default=0)

    class Children(ManySchema):
        parent_id = ForeignKeyField("Parents")

        @trigger(OpCode.INSERT)
        def inspect(self, tx: Transaction, ctx: TriggerContext) -> None:
            if required:
                ctx.require_foreignkey("parent_id")
            else:
                observed.append(ctx.resolve_foreignkey("parent_id"))

    store = make_store(tmp_path, Parents, Children, root_schema=Root)
    result = store.execute(
        (setvalue(Root, number=1), insert(Children, "child")),
        transaction_id="nullable-reference",
    )
    assert result.success is not required
    assert not store.is_shutdown
    if required:
        assert isinstance(result.error, InvalidOperationError)
        assert store.snapshot_by_schema("Root", key="number") == 0
        assert store.snapshot_by_schema("Children") == {}
    else:
        assert observed == [None]
        assert store.snapshot_by_schema("Children", id="child") == {
            "parent_id": None
        }


def test_remove_context_has_before_row_and_no_current_value(
    tmp_path: Path,
) -> None:
    observed = []

    class Children(ManySchema):
        parent_id = ForeignKeyField("Parents")

        @trigger(OpCode.REMOVE)
        def inspect(self, tx: Transaction, ctx: TriggerContext) -> None:
            assert ctx.id == "child"
            assert ctx.key is None
            assert ctx.value is None
            assert ctx.row(ctx.node, "child") is None
            assert ctx.row(ctx.node, "child", through=ctx.operation) is None
            reference = ctx.require_foreignkey("parent_id")
            observed.append((ctx.current(), reference.row["number"]))

    store = make_store(tmp_path, Parents, Children)
    store.execute(
        (
            insert(Parents, "parent"),
            insert(Children, "child", parent_id="parent"),
        ),
        transaction_id="seed",
    )
    result = store.execute(
        (
            setrow(Parents, "parent", number=7),
            RemoveRequestAction("Children", "child"),
        ),
        transaction_id="remove",
    )
    assert result.success
    assert observed == [({"parent_id": "parent"}, 7)]


def test_context_rejects_invalid_public_resolution_boundaries(
    tmp_path: Path,
) -> None:
    previous = []

    class Root(SingleSchema):
        number = NumericField(default=0)

        @trigger(OpCode.SET)
        def inspect(self, tx: Transaction, ctx: TriggerContext) -> None:
            parents = ctx.engine.get_node_by_schema("Parents")
            with pytest.raises(TypeError, match="ForeignKeyField"):
                ctx.resolve_foreignkey("number")
            with pytest.raises(TypeError, match="ManySchema"):
                ctx.row(ctx.node, "unused")
            with pytest.raises(TypeError, match="SingleSchema"):
                ctx.single(parents)
            with pytest.raises(ValueError, match="not both"):
                ctx.single(
                    ctx.node, before=ctx.operation, through=ctx.operation
                )
            with pytest.raises(ValueError, match="not both"):
                ctx.row(
                    parents, "a", before=ctx.operation, through=ctx.operation
                )
            if previous:
                with pytest.raises(ValueError, match="not visible"):
                    ctx.single(ctx.node, before=previous[0])
                with pytest.raises(ValueError, match="not visible"):
                    ctx.row(parents, "a", through=previous[0])
            previous.append(ctx.operation)

    store = make_store(tmp_path, Parents, root_schema=Root)
    for number in (1, 2):
        result = store.execute(
            (setvalue(Root, number=number),), transaction_id=str(number)
        )
        assert result.success
    assert store.snapshot_by_schema("Root", key="number") == 2


def test_trigger_builder_combines_context_routed_commands_atomically(
    tmp_path: Path,
) -> None:
    class Root(SingleSchema):
        number = NumericField(default=0)

    class Audit(ManySchema):
        number = NumericField(default=0)

    class Children(ManySchema):
        parent_id = ForeignKeyField("Parents")

        @trigger(OpCode.INSERT)
        def react(self, tx: Transaction, ctx: TriggerContext) -> None:
            reference = ctx.require_foreignkey("parent_id")
            root = ctx.engine.get_node_by_schema("Root")
            audit = ctx.engine.get_node_by_schema("Audit")
            tx.set(root.address, "number", reference.row["number"])
            tx.setrow(reference.node.address, reference.id, "number", 9)
            tx.insert(audit.address, {"number": reference.row["number"]})
            tx.remove(reference.node.address, "obsolete")

    store = make_store(tmp_path, Parents, Audit, Children, root_schema=Root)
    result = store.execute(
        (
            insert(Parents, "parent", number=3),
            insert(Parents, "obsolete"),
            insert(Children, "child", parent_id="parent"),
        ),
        transaction_id="mixed-trigger-commands",
    )
    assert result.success
    assert store.snapshot_by_schema("Root", key="number") == 3
    assert store.snapshot_by_schema("Parents", id="parent", key="number") == 9
    assert store.snapshot_by_schema("Parents", id="obsolete") is None
    assert list(store.snapshot_by_schema("Audit").values()) == [{"number": 3}]
