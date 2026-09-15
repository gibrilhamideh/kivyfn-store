from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.builder import Branch, Builder
from kivyfn.store.error import (
    ForeignKeyNotFoundError,
    ForeignKeyRestrictedError,
    NodeNotFoundError,
    RowNotFoundError,
)
from kivyfn.store.operations import OpCode, OperationSource
from kivyfn.store.protocol.message.requests import RemoveRequestAction
from kivyfn.store.schema import (
    DataField,
    ForeignKeyField,
    ForeignKeyPolicy,
    ManySchema,
    NumericField,
    SingleSchema,
    trigger,
)
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.store import Store
from kivyfn.store.transaction import Transaction
from tests.core.helpers import insert, make_store, setrow, setvalue


class Parents(ManySchema):
    number = NumericField(default=0)


class Children(ManySchema):
    parent_id = ForeignKeyField("Parents", on_delete=ForeignKeyPolicy.CASCADE)
    other_id = ForeignKeyField("Parents", on_delete=ForeignKeyPolicy.CASCADE)


class Selection(SingleSchema):
    parent_id = ForeignKeyField("Parents")


def test_reparent_before_delete_preserves_child(tmp_path: Path) -> None:
    store = make_store(tmp_path, Parents, Children)
    assert store.execute(
        (
            insert(Parents, "a"),
            insert(Parents, "b"),
            insert(Children, "child", parent_id="a"),
        ),
        transaction_id="seed",
    ).success
    actions = (
        setrow(Children, "child", parent_id="b"),
        RemoveRequestAction("Parents", "a"),
    )
    assert store.execute(actions, transaction_id="reparent").success
    assert store.snapshot_by_schema("Children", id="child") == {
        "parent_id": "b",
        "other_id": None,
    }


@pytest.mark.parametrize("explicit_remove", [True, False])
def test_duplicate_cascade_paths_delete_child_exactly_once(
    tmp_path: Path, explicit_remove: bool
) -> None:
    store = make_store(tmp_path, Parents, Children)
    store.execute(
        (
            insert(Parents, "a"),
            insert(Children, "c", parent_id="a", other_id="a"),
        ),
        transaction_id="seed",
    )
    actions = [RemoveRequestAction("Parents", "a")]
    if explicit_remove:
        actions.append(RemoveRequestAction("Children", "c"))
    result = store.execute(tuple(actions), transaction_id="cascade")
    assert result.success
    assert result.commit is not None
    assert len(result.commit.events) == 2
    assert store.snapshot_by_schema("Children") == {}


def test_child_inserted_before_parent_deletion_is_cascaded(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Parents, Children)
    store.execute((insert(Parents, "a"),), transaction_id="seed")
    result = store.execute(
        (
            insert(Children, "c", parent_id="a"),
            RemoveRequestAction("Parents", "a"),
        ),
        transaction_id="new-child",
    )
    assert result.success
    assert store.snapshot_by_schema("Children") == {}


def test_remove_reinsert_parent_deletes_old_cascade_children(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Parents, Children)
    store.execute(
        (insert(Parents, "a"), insert(Children, "c", parent_id="a")),
        transaction_id="seed",
    )
    assert store.execute(
        (RemoveRequestAction("Parents", "a"), insert(Parents, "a", number=2)),
        transaction_id="replace",
    ).success
    assert store.snapshot_by_schema("Children", id="c") is None


def test_self_and_cyclic_cascades_terminate(tmp_path: Path) -> None:
    class Links(ManySchema):
        parent_id = ForeignKeyField(
            "Links", on_delete=ForeignKeyPolicy.CASCADE
        )
        other_id = ForeignKeyField("Links", on_delete=ForeignKeyPolicy.CASCADE)

    store = make_store(tmp_path, Links)
    assert store.execute(
        (
            insert(Links, "a", parent_id="b", other_id="a"),
            insert(Links, "b", parent_id="a"),
            insert(Links, "c", parent_id="b"),
        ),
        transaction_id="seed",
    ).success
    result = store.execute(
        (RemoveRequestAction("Links", "a"),), transaction_id="cycle"
    )
    assert result.success
    assert result.commit is not None
    assert len(result.commit.events) == 3
    assert store.snapshot_by_schema("Links") == {}


def test_single_fk_rejects_missing_reference_without_shutdown(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Parents, Selection)
    result = store.execute(
        (setvalue(Selection, parent_id="missing"),), transaction_id="missing"
    )
    assert isinstance(result.error, ForeignKeyNotFoundError)
    assert store.snapshot_by_schema("Selection") == {"parent_id": None}
    assert not store.is_shutdown


@pytest.mark.parametrize("clear_first", [True, False])
def test_single_fk_restrict_uses_final_value(
    tmp_path: Path, clear_first: bool
) -> None:
    store = make_store(tmp_path, Parents, Selection)
    assert store.execute(
        (insert(Parents, "a"), setvalue(Selection, parent_id="a")),
        transaction_id="seed",
    ).success
    failed = store.execute(
        (RemoveRequestAction("Parents", "a"),), transaction_id="restricted"
    )
    assert isinstance(failed.error, ForeignKeyRestrictedError)
    actions = (
        setvalue(Selection, parent_id=None),
        RemoveRequestAction("Parents", "a"),
    )
    if not clear_first:
        actions = actions[::-1]
    assert store.execute(actions, transaction_id="clear").success
    assert store.snapshot_by_schema("Parents") == {}


def test_single_unrelated_fk_does_not_block_delete(tmp_path: Path) -> None:
    store = make_store(tmp_path, Parents, Selection)
    assert store.execute(
        (
            insert(Parents, "a"),
            insert(Parents, "b"),
            setvalue(Selection, parent_id="b"),
        ),
        transaction_id="seed",
    ).success
    assert store.execute(
        (RemoveRequestAction("Parents", "a"),), transaction_id="remove"
    ).success


def test_single_cascade_is_rejected_at_metadata_construction(
    tmp_path: Path,
) -> None:
    class Unsupported(SingleSchema):
        parent_id = ForeignKeyField(
            "Parents", on_delete=ForeignKeyPolicy.CASCADE
        )

    with pytest.raises(
        ValueError, match="singleton records cannot be deleted"
    ):
        make_store(tmp_path, Parents, Unsupported)


def test_foreign_key_target_must_be_collection(tmp_path: Path) -> None:
    class Singleton(SingleSchema):
        pass

    class Referencing(ManySchema):
        parent_id = ForeignKeyField("Singleton")

    with pytest.raises(ValueError, match="must reference a ManySchema"):
        make_store(tmp_path, Singleton, Referencing)


def test_root_trigger_reads_logical_values_and_mutates_root(
    tmp_path: Path,
) -> None:
    class Root(SingleSchema):
        number = NumericField(default=0)
        mirror = NumericField(default=0)

        @trigger(OpCode.SET, field="number")
        def mirror_number(self, tx: Transaction, ctx: TriggerContext):
            tx.set(ctx.node.address, "mirror", ctx.current()["number"])

    store = make_store(tmp_path, root_schema=Root)
    result = store.execute((setvalue(Root, number=7),), transaction_id="root")
    assert result.success
    assert store.snapshot_by_schema("Root") == {"number": 7, "mirror": 7}
    assert result.commit is not None
    assert result.commit.events[-1].source == OperationSource.TRIGGER


def test_missing_row_is_rejected_before_trigger_context_read(
    tmp_path: Path,
) -> None:
    calls = []

    class Triggered(ManySchema):
        number = NumericField(default=0)

        @trigger(OpCode.SETROW)
        @trigger(OpCode.REMOVE)
        def current(self, tx: Transaction, ctx: TriggerContext):
            calls.append(ctx.current())

    store = make_store(tmp_path, Triggered)
    for action in (
        setrow(Triggered, "missing", number=1),
        RemoveRequestAction("Triggered", "missing"),
    ):
        result = store.execute((action,), transaction_id="missing")
        assert isinstance(result.error, RowNotFoundError)
        assert not store.is_shutdown
    assert calls == []


def test_trigger_current_before_through_and_nested_values_are_detached(
    tmp_path: Path,
) -> None:
    observations: list[tuple[OpCode, dict[str, Any]]] = []

    class Triggered(ManySchema):
        number = NumericField(default=0)
        payload = DataField[dict[str, Any]](default={"items": []})

        @trigger(OpCode.INSERT)
        @trigger(OpCode.SETROW)
        @trigger(OpCode.REMOVE)
        def observe(self, tx: Transaction, ctx: TriggerContext):
            current = ctx.current()
            observations.append((ctx.opcode, current))
            current["payload"]["items"].append("callback")
            if ctx.opcode is OpCode.SETROW:
                assert ctx.id is not None
                before = ctx.row(ctx.node, ctx.id, before=ctx.operation)
                assert before is not None
                assert before["number"] == 0
                assert current["number"] == 2

    store = make_store(tmp_path, Triggered)
    result = store.execute(
        (insert(Triggered, "a"), setrow(Triggered, "a", number=2)),
        transaction_id="triggers",
    )
    assert result.success
    assert store.snapshot_by_schema("Triggered", id="a") == {
        "number": 2,
        "payload": {"items": []},
    }
    assert [op for op, _ in observations] == [OpCode.INSERT, OpCode.SETROW]
    assert store.execute(
        (RemoveRequestAction("Triggered", "a"),), transaction_id="remove"
    ).success
    assert observations[-1][1]["number"] == 2


def test_trigger_fk_resolution_failure_is_domain_failure(
    tmp_path: Path,
) -> None:
    class Triggered(ManySchema):
        parent_id = ForeignKeyField("Parents")

        @trigger(OpCode.INSERT)
        def resolve(self, tx: Transaction, ctx: TriggerContext):
            ctx.require_foreignkey("parent_id")

    store = make_store(tmp_path, Parents, Triggered)
    result = store.execute(
        (insert(Triggered, "a", parent_id="missing"),), transaction_id="fk"
    )
    assert isinstance(result.error, ForeignKeyNotFoundError)
    assert not store.is_shutdown


def test_trigger_unknown_address_is_domain_failure(tmp_path: Path) -> None:
    from kivyfn.store.address import Address

    class Root(SingleSchema):
        number = NumericField(default=0)

        @trigger(OpCode.SET)
        def bad_target(self, tx: Transaction, ctx: TriggerContext):
            tx.set(Address("unknown"), "number", 1)

    store = make_store(tmp_path, root_schema=Root)
    result = store.execute((setvalue(Root, number=1),), transaction_id="bad")
    assert isinstance(result.error, NodeNotFoundError)
    assert store.snapshot_by_schema("Root", key="number") == 0
    assert not store.is_shutdown


def test_duplicate_schema_registration_is_rejected(tmp_path: Path) -> None:
    tree = (
        Branch(Selection)
        .child("a", Branch(Parents))
        .child("b", Branch(Parents))
    )
    root, config = Builder().tree(tree).storage_path(tmp_path).build()
    with pytest.raises(RuntimeError, match="attached to multiple nodes"):
        Store(root, config)


def test_cascade_trigger_removes_already_scheduled_policy_row(
    tmp_path: Path,
) -> None:
    class Descendants(ManySchema):
        parent_id = ForeignKeyField(
            "Parents", on_delete=ForeignKeyPolicy.CASCADE
        )

        @trigger(OpCode.REMOVE)
        def remove_sibling(self, tx: Transaction, ctx: TriggerContext):
            if ctx.id == "a":
                tx.remove(ctx.node.address, "b")

    store = make_store(tmp_path, Parents, Descendants)
    assert store.execute(
        (
            insert(Parents, "parent"),
            insert(Descendants, "a", parent_id="parent"),
            insert(Descendants, "b", parent_id="parent"),
        ),
        transaction_id="seed",
    ).success
    result = store.execute(
        (RemoveRequestAction("Parents", "parent"),), transaction_id="cascade"
    )
    assert result.success
    assert result.commit is not None
    assert len(result.commit.events) == 3
    assert store.snapshot_by_schema("Descendants") == {}


def test_recursive_trigger_expansion_fails_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kivyfn.store.core.compiler import Compiler
    from kivyfn.store.error import InvalidOperationError

    class Root(SingleSchema):
        value = NumericField(default=0)

        @trigger(OpCode.SET)
        def recurse(self, tx: Transaction, ctx: TriggerContext):
            tx.set(ctx.node.address, "value", ctx.current()["value"] + 1)

    monkeypatch.setattr(Compiler, "MAX_OPERATIONS", 16)
    store = make_store(tmp_path, root_schema=Root)
    result = store.execute((setvalue(Root, value=1),), transaction_id="loop")
    assert isinstance(result.error, InvalidOperationError)
    assert store.snapshot_by_schema("Root", key="value") == 0
    assert not store.is_shutdown


def test_many_restrict_relationship_uses_future_membership(
    tmp_path: Path,
) -> None:
    class References(ManySchema):
        parent_id = ForeignKeyField("Parents")

    store = make_store(tmp_path, Parents, References)
    assert store.execute(
        (insert(Parents, "a"), insert(References, "r", parent_id="a")),
        transaction_id="seed",
    ).success
    failure = store.execute(
        (RemoveRequestAction("Parents", "a"),), transaction_id="restricted"
    )
    assert isinstance(failure.error, ForeignKeyRestrictedError)
    assert store.execute(
        (
            RemoveRequestAction("Parents", "a"),
            RemoveRequestAction("References", "r"),
        ),
        transaction_id="both",
    ).success


def test_cascading_child_with_restricted_dependent_fails_atomically(
    tmp_path: Path,
) -> None:
    class Dependents(ManySchema):
        child_id = ForeignKeyField("Children")

    store = make_store(tmp_path, Parents, Children, Dependents)
    assert store.execute(
        (
            insert(Parents, "a"),
            insert(Children, "c", parent_id="a"),
            insert(Dependents, "d", child_id="c"),
        ),
        transaction_id="seed",
    ).success
    failure = store.execute(
        (RemoveRequestAction("Parents", "a"),), transaction_id="restricted"
    )
    assert isinstance(failure.error, ForeignKeyRestrictedError)
    assert store.snapshot_by_schema("Parents", id="a") is not None
    assert store.snapshot_by_schema("Children", id="c") is not None
    assert store.schema_revisions["Parents"] == 1


def test_future_fk_target_can_be_inserted_after_referencing_row(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Parents, Selection)
    result = store.execute(
        (setvalue(Selection, parent_id="new"), insert(Parents, "new")),
        transaction_id="future",
    )
    assert result.success
    assert store.snapshot_by_schema("Selection") == {"parent_id": "new"}


def test_delete_trigger_can_reparent_child_before_cascade(
    tmp_path: Path,
) -> None:
    class Owners(ManySchema):
        @trigger(OpCode.REMOVE)
        def preserve_child(self, tx: Transaction, ctx: TriggerContext):
            if ctx.id == "old":
                child = ctx.engine.get_node_by_schema("Owned")
                tx.setrow(child.address, "child", "owner_id", "new")

    class Owned(ManySchema):
        owner_id = ForeignKeyField(
            "Owners", on_delete=ForeignKeyPolicy.CASCADE
        )

    store = make_store(tmp_path, Owners, Owned)
    assert store.execute(
        (
            insert(Owners, "old"),
            insert(Owners, "new"),
            insert(Owned, "child", owner_id="old"),
        ),
        transaction_id="seed",
    ).success
    assert store.execute(
        (RemoveRequestAction("Owners", "old"),), transaction_id="preserve"
    ).success
    assert store.snapshot_by_schema("Owned", id="child") == {"owner_id": "new"}


def test_insert_trigger_commands_run_before_following_caller_instruction(
    tmp_path: Path,
) -> None:
    class Owners(ManySchema):
        @trigger(OpCode.INSERT)
        def create_child(self, tx: Transaction, ctx: TriggerContext):
            child = ctx.engine.get_node_by_schema("Owned")
            tx.insert(child.address, {"owner_id": ctx.id}, id=ctx.id)

    class Owned(ManySchema):
        owner_id = ForeignKeyField(
            "Owners", on_delete=ForeignKeyPolicy.CASCADE
        )
        number = NumericField(default=0)

    store = make_store(tmp_path, Owners, Owned)
    result = store.execute(
        (insert(Owners, "a"), setrow(Owned, "a", number=2)),
        transaction_id="insert-and-edit",
    )
    assert result.success
    assert store.snapshot_by_schema("Owned", id="a", key="number") == 2
    result = store.execute(
        (insert(Owners, "b"), RemoveRequestAction("Owners", "b")),
        transaction_id="insert-and-remove",
    )
    assert result.success
    assert store.snapshot_by_schema("Owned", id="b") is None
    result = store.execute(
        (RemoveRequestAction("Owners", "a"), insert(Owners, "a")),
        transaction_id="replace",
    )
    assert result.success
    assert store.snapshot_by_schema("Owned", id="a", key="number") == 0
