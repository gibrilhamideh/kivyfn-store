from __future__ import annotations

from kivyfn.store.address import Address
from kivyfn.store.node import Node
from kivyfn.store.schema import Schema


class Branch:
    """
    Build-time structural representation of a state node.

    This is NOT a runtime Node.
    It only describes the hierarchical structure
    used by Builder to construct the Store.
    """

    __slots__ = ("schema", "_children")

    def __init__(self, schema: type[Schema]):
        if not isinstance(schema, type) or not issubclass(schema, Schema):
            raise TypeError("Branch requires a Schema class")

        self.schema = schema
        self._children: dict[str, Branch] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def child(self, name: str, branch: Branch) -> Branch:
        """
        Attach a child branch under this branch.

        Returns self for fluent chaining.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("Child name must be a non-empty string")

        if not isinstance(branch, Branch):
            raise TypeError("child() requires a Branch instance")

        if name in self._children:
            raise ValueError(
                "Duplicate child name "
                f"{name!r} under schema "
                f"{self.schema.__name__!r}"
            )

        self._children[name] = branch
        return self

    # ------------------------------------------------------------------
    # Internal Accessors (used by Builder)
    # ------------------------------------------------------------------

    @property
    def children(self) -> dict[str, Branch]:
        """
        Read-only view for Builder usage.
        """
        return self._children.copy()

    def _raw_children(self) -> dict[str, Branch]:
        """
        Internal access without copy (Builder only).
        """
        return self._children

    def _build_node(self, path: tuple[str, ...] = ()) -> Node:
        """Construct fresh canonical Nodes from this validated definition."""
        node = Node(self.schema(), Address(*path))
        for name, branch in self._children.items():
            node.add_child(name, branch._build_node(path + (name,)))
        return node

    # ------------------------------------------------------------------
    # Debug Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"Branch(schema={self.schema.__name__}, "
            f"children={list(self._children)})"
        )
