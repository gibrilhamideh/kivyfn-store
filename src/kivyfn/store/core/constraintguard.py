from __future__ import annotations
from typing import Any
from .delta import Delta
from .engine import Engine
from .registry import ForeignKeyRegistry

from ..errors import ConstraintError
from ..constants import Policy, ConstraintType


class ConstraintGuard:
    """
    Enforces relational invariants against a Delta view.

    Validates:
        - Uniqueness
        - Foreign key existence
        - Restrict delete

    Does NOT mutate state.
    """

    def __init__(self, engine: Engine):
        self.engine = engine

    # ============================================================
    # Public API
    # ============================================================

    def validate(self, delta: Delta):
        self._validate_uniques(delta)
        self._validate_fk_existence(delta)
        self._validate_restrict(delta)

    # ============================================================
    # UNIQUE CONSTRAINTS
    # ============================================================
    def _validate_uniques(self, delta: Delta):
        for node in delta.touched_nodes:
            candidate_ids = delta.updated_ids(node) | delta.inserted_ids(node)

            for unique_key in node.schema.unique_keys():
                # Only validate this unique if one of its fields was touched
                if not set(unique_key) & delta.touched_fields(node):
                    continue

                seen: set[Any] = set()

                for id in candidate_ids:
                    if not delta.exists(node, id):
                        continue

                    value = self._build_unique_value(delta, node, id, unique_key)

                    if self._is_empty_unique_value(value):
                        continue

                    # 1. Collision inside same transaction
                    if value in seen:
                        raise ConstraintError(
                            message=(
                                f"Unique constraint violated on "
                                f"{node.schema.__name__}.{'.'.join(unique_key)}"
                            ),
                            constraint_type=ConstraintType.UNIQUE,
                            schema=node.schema,
                            field=".".join(unique_key),
                            value=value,
                        )

                    seen.add(value)

                    # 2. Collision with base state
                    existing_id = node.lookup(unique_key, value, first=True)

                    if not isinstance(existing_id, str) or existing_id == id:
                        continue

                    if not delta.exists(node, existing_id):
                        continue

                    existing_future_value = self._build_unique_value(
                        delta,
                        node,
                        existing_id,
                        unique_key,
                    )

                    if existing_future_value == value:
                        raise ConstraintError(
                            message=(
                                f"Unique constraint violated on "
                                f"{node.schema.__name__}.{'.'.join(unique_key)}"
                            ),
                            constraint_type=ConstraintType.UNIQUE,
                            schema=node.schema,
                            field=".".join(unique_key),
                            value=value,
                        )

    # ============================================================
    # FOREIGN KEY EXISTENCE
    # ============================================================
    def _validate_fk_existence(self, delta: Delta):
        for node in delta.touched_nodes:
            # Foreign keys where this node is the source
            fks = ForeignKeyRegistry.foreign_keys_of(node.schema.__name__)
            if not fks:
                continue

            for fk in fks:
                # Only validate if FK field was touched
                if fk.field not in delta.touched_fields(node):
                    continue

                target_node = self.engine.get_node_by_schema(fk.reference)

                # Only check changed rows
                candidate_ids = delta.inserted_ids(node) | delta.updated_ids(node)

                for id in candidate_ids:
                    foreignkey = delta.value(node, id, fk.field)

                    # NULL foreign keys allowed
                    if foreignkey is None:
                        continue

                    # Check logical existence in future state
                    if not delta.exists(target_node, foreignkey):
                        raise ConstraintError(
                            message=(
                                f"Foreign key constraint failed on "
                                f"{node.schema.__name__}.{fk.field} "
                                f"-> {fk.reference}"
                            ),
                            constraint_type=ConstraintType.FK_MISSING,
                            schema=node.schema,
                            field=fk.field,
                            value=foreignkey,
                        )

    # ============================================================
    # RESTRICT DELETE
    # ============================================================
    def _validate_restrict(self, delta: Delta):
        for target_node, removed_ids in delta._removed.items():
            fks = ForeignKeyRegistry.referenced_by(target_node.schema.__name__)

            for fk in fks:
                if fk.on_delete is not Policy.RESTRICT:
                    continue

                referencing_node = self.engine.get_node_by_schema(fk.schema)

                for removed_id in removed_ids:
                    referencing_ids = referencing_node.lookup(fk.field, removed_id)
                    if not isinstance(referencing_ids, set):
                        continue

                    for ref_id in referencing_ids:
                        # If referencing row is deleted in this transaction → safe
                        if not delta.exists(referencing_node, ref_id):
                            continue

                        future_value = delta.value(referencing_node, ref_id, fk.field)

                        if future_value == removed_id:
                            raise ConstraintError(
                                message=(
                                    f"Cannot delete {target_node.schema.__name__} "
                                    f"'{removed_id}' because it is still referenced "
                                    f"by {fk.schema}.{fk.field}"
                                ),
                                constraint_type=ConstraintType.FK_RESTRICT,
                                schema=target_node.schema,
                                field=fk.field,
                                value=removed_id,
                            )

    # ============================================================
    # Helpers
    # ============================================================
    def _build_unique_value(self, delta: Delta, node, id: str, fields: tuple[str, ...]):
        if len(fields) == 1:
            return delta.value(node, id, fields[0])

        return tuple(delta.value(node, id, field) for field in fields)

    def _is_empty_unique_value(self, value: Any) -> bool:
        if value is None:
            return True

        if isinstance(value, tuple):
            return any(part is None for part in value)

        return False
