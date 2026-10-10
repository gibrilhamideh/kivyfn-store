from __future__ import annotations

import json
import pickle
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.builder import Branch, Builder, HistoryConfig, Initializer
from kivyfn.store.builder.policy import BootPolicy
from kivyfn.store.core.compiler import Compiler
from kivyfn.store.core.delta import Delta
from kivyfn.store.core.instruction import RemoveInstruction
from kivyfn.store.core.operations import RemoveOperation
from kivyfn.store.error import (
    FieldNotFoundError,
    FieldValidationError,
    ForeignKeyRestrictedError,
    InvalidOperationError,
    UniqueConstraintError,
)
from kivyfn.store.operations import OpCode, OperationSource
from kivyfn.store.protocol.message.requests import RemoveRequestAction
from kivyfn.store.query import field
from kivyfn.store.schema import (
    ForeignKeyField,
    ForeignKeyPolicy,
    ManySchema,
    NumericField,
    Schema,
    SingleSchema,
)
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.trigger import trigger
from kivyfn.store.storage import FileStorage
from kivyfn.store.store import Store
from kivyfn.store.transaction import Transaction
from tests.core.helpers import insert, setrow


class Root(SingleSchema):
    pass


class Parents(ManySchema):
    persistent = True
    track_history = True
    level = NumericField(default=0)


class Children(ManySchema):
    persistent = True
    uniques = [("parent_id",)]
    parent_id = ForeignKeyField(
        "Parents", ensure=True, on_delete=ForeignKeyPolicy.CASCADE
    )
    value = NumericField(default=7)


class ConditionalChildren(Children):
    parent_id = ForeignKeyField(
        "Parents",
        ensure=True,
        ensure_where=field("level") >= 2,
        on_delete=ForeignKeyPolicy.CASCADE,
    )


def configured(
    path: Path,
    *schemas: type[Schema],
    policy: BootPolicy = BootPolicy.RAISE,
    initializer: Initializer | None = None,
) -> Store:
    tree = Branch(Root)
    for schema in schemas:
        tree.child(schema.__name__, Branch(schema))
    config = (
        Builder()
        .tree(tree)
        .storage_path(path)
        .boot_policy(policy.value)
        .initializer(initializer)
        .history(HistoryConfig())
        .build()
    )
    return Store(config)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    instance = configured(tmp_path, Parents, Children)
    instance.boot()
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.mark.parametrize("uniques", [[], [("parent_id", "value")]])
def test_ensure_requires_single_field_unique(
    tmp_path: Path, uniques: list[tuple[str, ...]]
) -> None:
    schema = type("InvalidChildren", (Children,), {"uniques": uniques})
    with pytest.raises(ValueError, match="single-field unique"):
        configured(tmp_path, Parents, schema)


def test_ensure_requires_collection_source(tmp_path: Path) -> None:
    class InvalidSingle(SingleSchema):
        uniques = [("parent_id",)]
        parent_id = ForeignKeyField("Parents", ensure=True)

    with pytest.raises(ValueError, match="ManySchema source"):
        configured(tmp_path, Parents, InvalidSingle)


@pytest.mark.parametrize(
    "options",
    [
        {"ensure": 1},
        {"ensure": True, "ensure_where": True},
        {"ensure": True, "ensure_where": lambda row: True},
        {"ensure_where": field("level") >= 1},
    ],
)
def test_invalid_field_options(options: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ForeignKeyField("Parents", **options)


@pytest.mark.parametrize("path", ["missing", "parent.level"])
def test_filter_is_validated_against_direct_parent_fields(
    tmp_path: Path, path: str
) -> None:
    class InvalidChildren(Children):
        parent_id = ForeignKeyField(
            "Parents", ensure=True, ensure_where=field(path) == 1
        )

    with pytest.raises((ValueError, FieldNotFoundError)):
        configured(tmp_path, Parents, InvalidChildren)


@pytest.mark.parametrize("self_reference", [False, True])
def test_ensure_cycles_fail_at_construction(
    tmp_path: Path, self_reference: bool
) -> None:
    class First(ManySchema):
        uniques = [("other",)]
        other = ForeignKeyField(
            "First" if self_reference else "Second", ensure=True
        )

    class Second(ManySchema):
        uniques = [("other",)]
        other = ForeignKeyField("First", ensure=True)

    with pytest.raises(ValueError, match="Cyclic ensure"):
        configured(tmp_path, First, Second)


def test_config_and_filter_metadata_are_serializable(tmp_path: Path) -> None:
    instance = configured(tmp_path, Parents, ConditionalChildren)
    config = pickle.loads(pickle.dumps(instance.config))
    restored = Store(config)
    restored.boot()
    assert restored.execute(
        (insert(Parents, "p", level=2),), transaction_id="create"
    ).success
    assert len(restored.snapshot_by_schema("ConditionalChildren")) == 1
    metadata = ConditionalChildren.parent_id.metadata
    assert json.loads(json.dumps(metadata))["ensure_where"] == {
        "type": "comparison",
        "field": ["level"],
        "operator": "gte",
        "value": 2,
    }
    restored.shutdown()


@pytest.mark.parametrize("persistent", [True, False])
def test_boot_backfills_and_preserves_custom_children(
    tmp_path: Path, persistent: bool
) -> None:
    calls = []

    class BootChildren(ConditionalChildren):
        @trigger(OpCode.INSERT)
        def inserted(self, tx: Transaction, ctx: TriggerContext) -> None:
            calls.append(ctx.id)

    BootChildren.persistent = persistent
    saved = {
        "Parents": {"old": {"level": 2}, "new": {"level": 3}, "off": {}},
        "BootChildren": {"custom": {"parent_id": "old", "value": 99}},
    }
    storage = FileStorage(tmp_path)
    storage.write_store(saved, store_version=1)
    first = configured(tmp_path, BootChildren, Parents)
    first.boot()
    children = first.snapshot_by_schema("BootChildren")
    assert {row["parent_id"] for row in children.values()} == {"old", "new"}
    if persistent:
        assert children["custom"]["value"] == 99
    else:
        assert all(row["value"] == 7 for row in children.values())
    assert calls == []
    assert set(first.schema_revisions.values()) == {0}
    assert first.history().status.total_entries == 0
    first.shutdown()
    manifest = storage._manifest_name()
    second = configured(tmp_path, BootChildren, Parents)
    second.boot()
    if persistent:
        assert second.snapshot_by_schema("BootChildren") == children
    assert storage._manifest_name() == manifest
    assert calls == []
    second.shutdown()


def test_initializer_runs_before_ensure(tmp_path: Path) -> None:
    class Seed(Initializer):
        def run(self, data: dict[str, Any]) -> dict[str, Any]:
            data["Parents"] = {"p": {}}
            data["Children"] = {"custom": {"parent_id": "p", "value": 25}}
            return data

    instance = configured(tmp_path, Parents, Children, initializer=Seed())
    instance.boot()
    assert instance.snapshot_by_schema("Children") == {
        "custom": {"parent_id": "p", "value": 25}
    }
    instance.shutdown()


def test_generated_children_are_policy_operations(store: Store) -> None:
    result = store.execute((insert(Parents, "p"),), transaction_id="create")
    assert result.success and result.commit is not None
    assert [event.source for event in result.commit.events] == [
        OperationSource.COMMAND,
        OperationSource.POLICY,
    ]
    children = store.snapshot_by_schema("Children")
    assert list(children.values()) == [{"parent_id": "p", "value": 7}]
    assert len(children) == 1


@pytest.mark.parametrize("child_first", [False, True])
def test_explicit_child_in_same_transaction_wins(
    store: Store, child_first: bool
) -> None:
    actions = [
        insert(Parents, "p"),
        insert(Children, "custom", parent_id="p", value=42),
    ]
    if child_first:
        actions.reverse()
    result = store.execute(tuple(actions), transaction_id="create")
    assert result.success and result.commit is not None
    assert len(result.commit.events) == 2
    assert store.snapshot_by_schema("Children") == {
        "custom": {"parent_id": "p", "value": 42}
    }


@pytest.mark.parametrize("duplicates", [False, True])
def test_parent_trigger_child_is_respected(
    tmp_path: Path, duplicates: bool
) -> None:
    class TriggerParents(Parents):
        @trigger(OpCode.INSERT)
        def inserted(self, tx: Transaction, ctx: TriggerContext) -> None:
            child = ctx.engine.get_node_by_schema("TriggerChildren")
            tx.insert(child.address, {"parent_id": ctx.id, "value": 99}, "x")
            if duplicates:
                tx.insert(child.address, {"parent_id": ctx.id}, "y")

    class TriggerChildren(Children):
        parent_id = ForeignKeyField("TriggerParents", ensure=True)

    instance = configured(tmp_path, TriggerParents, TriggerChildren)
    instance.boot()
    before = instance.checkpoint()
    result = instance.execute(
        (insert(TriggerParents, "p"),), transaction_id="create"
    )
    if duplicates:
        assert isinstance(result.error, UniqueConstraintError)
        assert instance.checkpoint() == before
    else:
        assert result.success and result.commit is not None
        assert len(result.commit.events) == 2
        assert instance.snapshot_by_schema("TriggerChildren") == {
            "x": {"parent_id": "p", "value": 99}
        }
    instance.shutdown()


def test_generated_insert_trigger_can_satisfy_another_pending_parent(
    tmp_path: Path,
) -> None:
    class TriggerChildren(Children):
        @trigger(OpCode.INSERT)
        def inserted(self, tx: Transaction, ctx: TriggerContext) -> None:
            if ctx.current()["parent_id"] == "a":
                child = ctx.engine.get_node_by_schema("TriggerChildren")
                tx.insert(child.address, {"parent_id": "b", "value": 99}, "x")

    instance = configured(tmp_path, Parents, TriggerChildren)
    instance.boot()
    result = instance.execute(
        (insert(Parents, "a"), insert(Parents, "b")), transaction_id="create"
    )
    assert result.success
    rows = instance.snapshot_by_schema("TriggerChildren")
    assert len(rows) == 2
    assert rows["x"] == {"parent_id": "b", "value": 99}
    instance.shutdown()


@pytest.mark.parametrize("boot", [False, True])
def test_ensure_chains_ignore_tree_order(tmp_path: Path, boot: bool) -> None:
    class Grandchildren(ManySchema):
        uniques = [("child_id",)]
        child_id = ForeignKeyField("Children", ensure=True)

    if boot:
        FileStorage(tmp_path).write_store(
            {"Parents": {"p": {}}}, store_version=1
        )
    instance = configured(tmp_path, Grandchildren, Children, Parents)
    instance.boot()
    if not boot:
        assert instance.execute(
            (insert(Parents, "p"),), transaction_id="create"
        ).success
    children = instance.snapshot_by_schema("Children")
    grandchildren = instance.snapshot_by_schema("Grandchildren")
    assert len(children) == len(grandchildren) == 1
    assert list(grandchildren.values()) == [{"child_id": next(iter(children))}]
    instance.shutdown()


def test_filter_uses_final_parent_values_and_preserves_existing_child(
    tmp_path: Path,
) -> None:
    instance = configured(tmp_path, Parents, ConditionalChildren)
    instance.boot()
    assert instance.execute(
        (insert(Parents, "p", level=2), setrow(Parents, "p", level=0)),
        transaction_id="off",
    ).success
    assert instance.snapshot_by_schema("ConditionalChildren") == {}
    assert instance.execute(
        (setrow(Parents, "p", level=2),), transaction_id="on"
    ).success
    rows = instance.snapshot_by_schema("ConditionalChildren")
    assert len(rows) == 1
    assert instance.execute(
        (setrow(Parents, "p", level=0),), transaction_id="off-again"
    ).success
    assert instance.snapshot_by_schema("ConditionalChildren") == rows
    assert instance.execute(
        (RemoveRequestAction("ConditionalChildren", next(iter(rows))),),
        transaction_id="remove-unrequired",
    ).success
    assert instance.snapshot_by_schema("ConditionalChildren") == {}
    instance.shutdown()


@pytest.mark.parametrize("reparent", [False, True])
def test_removing_required_link_is_rejected_atomically(
    store: Store, reparent: bool
) -> None:
    assert store.execute(
        (insert(Parents, "p"),), transaction_id="seed"
    ).success
    child_id = next(iter(store.snapshot_by_schema("Children")))
    before = store.checkpoint()
    commits = []
    store.subscribe_commits(commits.append)
    action = (
        setrow(Children, child_id, parent_id=None)
        if reparent
        else RemoveRequestAction("Children", child_id)
    )
    result = store.execute((action,), transaction_id="unlink")
    assert isinstance(result.error, ForeignKeyRestrictedError)
    assert store.checkpoint() == before
    assert commits == []


def test_replacing_required_child_is_allowed(store: Store) -> None:
    assert store.execute(
        (insert(Parents, "p"),), transaction_id="seed"
    ).success
    child_id = next(iter(store.snapshot_by_schema("Children")))
    result = store.execute(
        (
            RemoveRequestAction("Children", child_id),
            insert(Children, "replacement", parent_id="p", value=12),
        ),
        transaction_id="replace",
    )
    assert result.success
    assert store.snapshot_by_schema("Children") == {
        "replacement": {"parent_id": "p", "value": 12}
    }


def test_cascade_and_parent_replacement_follow_final_lifetime(
    store: Store,
) -> None:
    assert store.execute(
        (insert(Parents, "p"),), transaction_id="seed"
    ).success
    old = store.snapshot_by_schema("Children")
    assert store.execute(
        (RemoveRequestAction("Parents", "p"), insert(Parents, "p")),
        transaction_id="replace-parent",
    ).success
    new = store.snapshot_by_schema("Children")
    assert len(new) == 1 and not (new.keys() & old.keys())
    assert store.execute(
        (RemoveRequestAction("Parents", "p"),), transaction_id="delete"
    ).success
    assert store.snapshot_by_schema("Children") == {}
    assert store.execute(
        (insert(Parents, "q"), RemoveRequestAction("Parents", "q")),
        transaction_id="temporary-parent",
    ).success
    assert store.snapshot_by_schema("Children") == {}


def test_undo_redo_restore_generated_child_identity(store: Store) -> None:
    assert store.execute(
        (insert(Parents, "p"),), transaction_id="create"
    ).success
    children = store.snapshot_by_schema("Children")
    assert store.undo(
        transaction_id="undo",
        expected_position=store.history().status.undo_position,
    ).success
    assert store.snapshot_by_schema("Parents") == {}
    assert store.snapshot_by_schema("Children") == {}
    assert store.redo(
        transaction_id="redo",
        expected_position=store.history().status.redo_position,
    ).success
    assert store.snapshot_by_schema("Children") == children


def test_invalid_defaults_fail_without_publishing_or_erasing(
    tmp_path: Path,
) -> None:
    class InvalidChildren(Children):
        value = NumericField()

    instance = configured(tmp_path, Parents, InvalidChildren)
    instance.boot()
    before = instance.checkpoint()
    result = instance.execute(
        (insert(Parents, "p"),), transaction_id="invalid"
    )
    assert isinstance(result.error, FieldValidationError)
    assert instance.checkpoint() == before
    instance.shutdown()

    storage = FileStorage(tmp_path)
    storage.write_store({"Parents": {"p": {}}}, store_version=1)
    saved = storage.load_store([])
    instance = configured(
        tmp_path, Parents, InvalidChildren, policy=BootPolicy.ERASE
    )
    with pytest.raises(FieldValidationError):
        instance.boot()
    assert storage.load_store([]) == saved


def test_generated_insert_feedback_uses_operation_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LoopChildren(Children):
        @trigger(OpCode.INSERT)
        def inserted(self, tx: Transaction, ctx: TriggerContext) -> None:
            assert ctx.id is not None
            child = ctx.engine.get_node_by_schema("LoopChildren")
            tx.remove(child.address, ctx.id)

    instance = configured(tmp_path, Parents, LoopChildren)
    instance.boot()
    before = instance.checkpoint()
    monkeypatch.setattr(Compiler, "MAX_OPERATIONS", 6)
    result = instance.execute((insert(Parents, "p"),), transaction_id="loop")
    assert isinstance(result.error, InvalidOperationError)
    assert instance.checkpoint() == before
    instance.shutdown()


def test_history_restore_cannot_bypass_required_relationship(store: Store):
    assert store.execute(
        (insert(Parents, "p"),), transaction_id="seed"
    ).success
    node = store.get_node_by_schema("Children")
    child_id = next(iter(node.value))
    delta = Delta(
        [
            RemoveOperation(
                node,
                RemoveInstruction(
                    address=node.address,
                    id=child_id,
                    source=OperationSource.POLICY,
                ),
            )
        ]
    )
    before = store.checkpoint()
    result = store._engine.restore(delta, transaction_id="restore")
    assert isinstance(result.error, ForeignKeyRestrictedError)
    assert store.checkpoint() == before


def test_swapping_child_links_uses_final_transaction_values(store: Store):
    assert store.execute(
        (
            insert(Parents, "a"),
            insert(Parents, "b"),
            insert(Children, "x", parent_id="a"),
            insert(Children, "y", parent_id="b"),
        ),
        transaction_id="seed",
    ).success
    assert store.execute(
        (
            setrow(Children, "x", parent_id="b"),
            setrow(Children, "y", parent_id="a"),
        ),
        transaction_id="swap",
    ).success
    assert store.snapshot_by_schema("Children") == {
        "x": {"parent_id": "b", "value": 7},
        "y": {"parent_id": "a", "value": 7},
    }


def test_unrelated_child_updates_do_not_reevaluate_parent_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = configured(tmp_path, Parents, ConditionalChildren)
    instance.boot()
    assert instance.execute(
        (insert(Parents, "p", level=2),), transaction_id="seed"
    ).success
    child_id = next(iter(instance.snapshot_by_schema("ConditionalChildren")))

    def unexpected(*args: Any) -> bool:
        pytest.fail("Unrelated child values must not evaluate ensure filters")

    monkeypatch.setattr(
        "kivyfn.store.core.ensure.evaluate_expression", unexpected
    )
    assert instance.execute(
        (setrow(ConditionalChildren, child_id, value=20),),
        transaction_id="reading",
    ).success
    instance.shutdown()


def test_recovery_removes_invalid_parents_before_ensuring_children(
    tmp_path: Path,
) -> None:
    class Other(ManySchema):
        pass

    class SavedParents(Parents):
        other_id = ForeignKeyField("Other")

    class SavedChildren(Children):
        parent_id = ForeignKeyField("SavedParents", ensure=True)

    FileStorage(tmp_path).write_store(
        {"SavedParents": {"bad": {"other_id": "missing"}, "good": {}}},
        store_version=1,
    )
    instance = configured(
        tmp_path, Other, SavedParents, SavedChildren, policy=BootPolicy.RECOVER
    )
    instance.boot()
    assert set(instance.snapshot_by_schema("SavedParents")) == {"good"}
    rows = instance.snapshot_by_schema("SavedChildren")
    assert list(rows.values()) == [{"parent_id": "good", "value": 7}]
    instance.shutdown()


def test_generated_trigger_can_replace_an_existing_required_child(
    tmp_path: Path,
) -> None:
    class TriggerChildren(Children):
        @trigger(OpCode.INSERT)
        def inserted(self, tx: Transaction, ctx: TriggerContext) -> None:
            if ctx.current()["parent_id"] == "b":
                tx.insert(ctx.node.address, {"parent_id": "a"}, "replacement")

    instance = configured(tmp_path, Parents, TriggerChildren)
    instance.boot()
    assert instance.execute(
        (insert(Parents, "a"),), transaction_id="seed"
    ).success
    child_id = next(iter(instance.snapshot_by_schema("TriggerChildren")))
    result = instance.execute(
        (
            RemoveRequestAction("TriggerChildren", child_id),
            insert(Parents, "b"),
        ),
        transaction_id="replace",
    )
    assert result.success
    rows = instance.snapshot_by_schema("TriggerChildren")
    assert len(rows) == 2
    assert rows["replacement"]["parent_id"] == "a"
    instance.shutdown()
