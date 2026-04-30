# foundation/state/builder/builder.py

from __future__ import annotations
from typing import Optional, Set

from .branch import Branch
from .config import StoreConfig

from ..node import Node


class Builder:
    """
    Responsible for constructing a Store from a Branch structure.

    Builder handles:
        - Structural validation (tree correctness)
        - Converting Branch → runtime Node tree
        - Attaching storage backend
        - Attaching boot policy
    """

    __slots__ = (
        "_root_branch",
        "_boot_policy",
        "_storage",
        "_built",
    )

    def __init__(self):
        self._root_branch: Optional[Branch] = None
        self._boot_policy = None
        self._storage = None
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

    def storage(self, storage_backend) -> Builder:
        """
        Attach a storage backend (e.g. FileStorage).
        """
        if self._storage is not None:
            raise RuntimeError("Storage already configured")

        self._storage = storage_backend
        return self

    # --------------------------------------------------------
    # Boot Policy Configuration
    # --------------------------------------------------------

    def boot_policy(self, policy) -> Builder:
        if self._boot_policy is not None:
            raise RuntimeError("Policy already configured")

        self._boot_policy = policy
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

        config = StoreConfig(self._storage, self._boot_policy)

        self._built = True

        return root_node, config

    # --------------------------------------------------------
    # Internal Structure Validation
    # --------------------------------------------------------
    def _validate_structure(self, root: Branch):
        visited: Set[int] = set()
        self._dfs_validate(root, visited)

    def _dfs_validate(self, branch: Branch, visited: Set[int]):
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
    # Branch → Node Conversion
    # --------------------------------------------------------

    def _build_node(self, branch: Branch) -> Node:
        node = Node(branch.schema)

        for name, child_branch in branch._raw_children().items():
            child_node = self._build_node(child_branch)
            node.add_child(name, child_node)

        return node
