from collections import defaultdict

from ..node import Node
from ..errors import CompilerError

from .operations import (
    InsertOperation,
    RemoveOperation,
    SetRowOperation,
)


class Delta:
    """
    Represents the normalized net row-level change of a transaction.

    Responsibilities:
        - Normalize row operations
        - Enforce structural consistency
        - Provide logical resolution API

    Does NOT validate relational constraints.
    """

    # ============================================================
    # Init
    # ============================================================

    def __init__(self, engine, operations):
        self.engine = engine

        # node -> {id -> full row}
        self._inserted = defaultdict(dict)

        # node -> set[id]
        self._removed = defaultdict(set)

        # node -> {id -> {field -> value}}
        self._updated = defaultdict(lambda: defaultdict(dict))

        # metadata
        self._touched_nodes: set[Node] = set()
        self._touched_fields = defaultdict(set)

        for op in operations:
            self._touched_nodes.add(op.node)

            if isinstance(op, InsertOperation):
                self._insert(op.node, op.id, op.value)

            elif isinstance(op, RemoveOperation):
                self._remove(op.node, op.id)

            elif isinstance(op, SetRowOperation):
                self._update(op.node, op.id, op.key, op.new_value)

    # ============================================================
    # Structural Rules
    # ============================================================
    def _insert(self, node, id, row):
        # Case 1: Insert after remove → treat as replace
        if id in self._removed[node]:
            self._removed[node].remove(id)

        # Case 2: Duplicate insert in same transaction
        if id in self._inserted[node]:
            raise CompilerError(f"Duplicate insert id '{id}' in same transaction")

        # Case 3: Insert existing base id (unless removed first)
        if id in node.value and id not in self._removed[node]:
            raise CompilerError(f"Insert id '{id}' already exists")

        self._inserted[node][id] = dict(row)

        for field in row:
            self._touched_fields[node].add(field)

    def _remove(self, node, id):
        # Removing inserted row cancels insertion
        if id in self._inserted[node]:
            del self._inserted[node][id]
            return

        # Duplicate remove
        if id in self._removed[node]:
            raise CompilerError(f"Duplicate remove id '{id}' in same transaction")

        # Removing non-existent row
        if id not in node.value:
            raise CompilerError(f"Cannot remove non-existent id '{id}'")

        self._removed[node].add(id)

        # Drop pending updates
        self._updated[node].pop(id, None)

    def _update(self, node, id, field, value):
        self._touched_fields[node].add(field)

        # Update inserted row
        if id in self._inserted[node]:
            self._inserted[node][id][field] = value
            return

        # Update removed row
        if id in self._removed[node]:
            raise CompilerError(f"Cannot update removed id '{id}'")

        # Update non-existent row
        if id not in node.value:
            raise CompilerError(f"Cannot update non-existent id '{id}'")

        self._updated[node][id][field] = value

    # ============================================================
    # Logical Resolution API
    # ============================================================

    def exists(self, node, id):
        """
        Logical existence after delta applied.
        """
        if id in self._inserted[node]:
            return True
        if id in self._removed[node]:
            return False
        return id in node.value

    def value(self, node, id, field):
        """
        Logical field value after delta applied.
        """

        # Inserted row overrides everything
        if id in self._inserted[node]:
            return self._inserted[node][id].get(field)

        # Removed row does not exist
        if id in self._removed[node]:
            return None

        # Updated field
        updated_fields = self._updated[node].get(id)
        if updated_fields and field in updated_fields:
            return updated_fields[field]

        # Fallback to base state
        row = node.value.get(id)
        return row.get(field) if row else None

    def ids(self, node):
        """
        Logical id set after delta applied.
        """
        base_ids = set(node.value.keys())

        if self._removed[node]:
            base_ids -= self.removed_ids(node)

        if self._inserted[node]:
            base_ids |= self.inserted_ids(node)

        return base_ids

    def removed_ids(self, node: Node) -> set:
        return set(self._removed[node])

    def inserted_ids(self, node: Node) -> set:
        return set(self._inserted[node].keys())

    def updated_ids(self, node: Node) -> set:
        return set(self._updated[node].keys())

    # ============================================================
    # Metadata
    # ============================================================

    @property
    def touched_nodes(self):
        return self._touched_nodes

    def touched_fields(self, node: Node) -> set[str]:
        return self._touched_fields[node]
