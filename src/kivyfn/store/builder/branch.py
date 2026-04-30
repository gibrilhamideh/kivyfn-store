from __future__ import annotations
from typing import Type


class Branch:
    """
    Build-time structural representation of a state node.

    This is NOT a runtime Node.
    It only describes the hierarchical structure
    used by Builder to construct the Store.
    """

    __slots__ = ("schema", "_children")

    def __init__(self, schema: Type):
        if schema is None:
            raise ValueError("Branch requires a schema")

        self.schema: Type = schema
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
                f"Duplicate child name '{name}' under schema '{self.schema.__name__}'"
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

    # ------------------------------------------------------------------
    # Debug Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"Branch(schema={self.schema.__name__}, children={list(self._children.keys())})"
