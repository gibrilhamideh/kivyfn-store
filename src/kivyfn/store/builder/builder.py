from __future__ import annotations

from pathlib import Path
from typing import Optional, Set, Literal

from kivyfn.store.address import Address
from kivyfn.store.builder.branch import Branch
from kivyfn.store.builder.config import StoreConfig
from kivyfn.store.builder.migration import Migration
from kivyfn.store.builder.policy import BootPolicy
from kivyfn.store.node import Node


class Builder:
    __slots__ = (
        "_root_branch",
        "_storage_path",
        "_store_version",
        "_boot_policy",
        "_migrations",
        "_built",
    )

    def __init__(self) -> None:
        self._root_branch: Optional[Branch] = None

        self._storage_path: Path = Path("./storage")
        self._store_version = 1
        self._boot_policy = BootPolicy.RAISE
        self._migrations: list[Migration] = []

        self._built = False

    # --------------------------------------------------------
    # Tree Definition
    # --------------------------------------------------------

    def tree(self, root: Branch) -> Builder:
        if self._root_branch is not None:
            raise RuntimeError("Tree already defined")

        if not isinstance(root, Branch):
            raise TypeError("tree() requires a Branch instance")

        self._root_branch = root
        return self

    # --------------------------------------------------------
    # Storage Configuration
    # --------------------------------------------------------

    def storage_path(self, path: str | Path) -> Builder:
        self._storage_path = Path(path)
        return self

    # --------------------------------------------------------
    # Migration Configuration
    # --------------------------------------------------------

    def store_version(self, version: int) -> Builder:
        if not isinstance(version, int):
            raise TypeError("store_version must be an int")

        if version < 1:
            raise ValueError("store_version must be >= 1")

        self._store_version = version
        return self

    def boot_policy(self, policy: Literal["erase", "raise"]) -> Builder:
        boot_policy = BootPolicy(policy)

        self._boot_policy = boot_policy
        return self

    def migrations(self, migrations: list[Migration]) -> Builder:
        self._migrations = list(migrations)
        return self

    # --------------------------------------------------------
    # Build Store
    # --------------------------------------------------------

    def build(self) -> tuple[Node, StoreConfig]:
        if self._built:
            raise RuntimeError("Builder can only build once")

        if self._root_branch is None:
            raise RuntimeError("No tree defined. Call .tree() first.")


        self._validate_structure(self._root_branch)

        root_node = self._build_node(self._root_branch)

        config = StoreConfig(
            storage_path=self._storage_path,
            store_version=self._store_version,
            boot_policy=self._boot_policy,
            migrations=self._migrations,
        )

        self._built = True
        return root_node, config

    # --------------------------------------------------------
    # Internal Structure Validation
    # --------------------------------------------------------

    def _validate_structure(self, root: Branch) -> None:
        visited: Set[int] = set()
        self._dfs_validate(root, visited)

    def _dfs_validate(self, branch: Branch, visited: Set[int]) -> None:
        branch_id = id(branch)

        if branch_id in visited:
            raise RuntimeError(
                f"Cycle detected in Branch structure at schema "
                f"'{branch.schema.__name__}'"
            )

        visited.add(branch_id)

        for child in branch._raw_children().values():
            self._dfs_validate(child, visited)

        visited.remove(branch_id)

    # --------------------------------------------------------
    # Branch -> Node Conversion
    # --------------------------------------------------------

    def _build_node(
        self,
        branch: Branch,
        path: tuple[str, ...] = (),
    ) -> Node:
        address = Address(*path)
        node = Node(branch.schema(), address)

        for name, child_branch in branch._raw_children().items():
            child_node = self._build_node(
                child_branch,
                path + (name,),
            )
            node.add_child(name, child_node)

        return node
