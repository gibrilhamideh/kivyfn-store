from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.address import Address
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.builder.migration import Migration
from kivyfn.store.builder.policy import BootPolicy
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.store import Store


class Root(SingleSchema):
    pass


class Child(ManySchema):
    pass


def test_builder_tree_addresses_config_and_single_build(
    tmp_path: Path,
) -> None:
    branch = Branch(Root).child("children", Branch(Child))
    migrations = [Migration()]
    builder = (
        Builder()
        .tree(branch)
        .storage_path(tmp_path)
        .store_version(3)
        .boot_policy("recover")
        .migrations(migrations)
    )
    config = builder.build()
    store = Store(config)
    root = store._engine.get_node_by_address(Address())
    assert root.address == Address()
    assert root.children["children"].address == Address("children")
    assert isinstance(root.schema, Root)
    assert isinstance(root.children["children"].schema, Child)
    assert config.storage_path == tmp_path
    assert config.store_version == 3
    assert config.boot_policy is BootPolicy.RECOVER
    migrations.clear()
    assert len(config.migrations) == 1
    builder.migrations([])
    assert len(config.migrations) == 1
    with pytest.raises(RuntimeError, match="once"):
        builder.build()


@pytest.mark.parametrize("version", [True, False, 1.5, "1", None])
def test_builder_rejects_noninteger_versions(version: Any) -> None:
    with pytest.raises(TypeError, match="int"):
        Builder().tree(Branch(Root)).store_version(version).build()


@pytest.mark.parametrize("version", [-1, 0])
def test_builder_rejects_nonpositive_version(version: int) -> None:
    with pytest.raises(ValueError, match=">= 1"):
        Builder().tree(Branch(Root)).store_version(version).build()


def test_builder_requires_tree_and_rejects_redefinition() -> None:
    builder = Builder()
    with pytest.raises(RuntimeError, match="No tree"):
        builder.build()
    builder.tree(Branch(Root))
    with pytest.raises(RuntimeError, match="already defined"):
        builder.tree(Branch(Root))
    with pytest.raises(ValueError):
        Builder().boot_policy("invalid")  # type: ignore[arg-type]


@pytest.mark.parametrize("schema", [None, Root(), "Root", int])
def test_branch_requires_schema_class(schema: Any) -> None:
    with pytest.raises(TypeError, match="Schema class"):
        Branch(schema)


def test_branch_children_snapshot_cannot_mutate_tree() -> None:
    branch = Branch(Root).child("child", Branch(Child))
    children = branch.children
    children.clear()
    assert list(branch.children) == ["child"]
    assert "Root" in repr(branch)
    assert "child" in repr(branch)


def test_branch_rejects_invalid_or_duplicate_children() -> None:
    branch = Branch(Root)
    with pytest.raises(ValueError, match="non-empty"):
        branch.child("", Branch(Child))
    with pytest.raises(TypeError, match="Branch"):
        branch.child("child", object())  # type: ignore[arg-type]
    branch.child("child", Branch(Child))
    with pytest.raises(ValueError, match="Duplicate"):
        branch.child("child", Branch(Child))


def test_builder_detects_branch_cycle_before_node_construction() -> None:
    branch = Branch(Root)
    child = Branch(Child)
    branch.child("child", child)
    child.child("root", branch)
    with pytest.raises(RuntimeError, match="Cycle"):
        Builder().tree(branch).build()
