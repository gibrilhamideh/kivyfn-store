# kivyfn/store/core/constraintguard.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from kivyfn.store.error import (
    ConstraintErrorDetail,
    ForeignKeyNotFoundError,
    ForeignKeyRestrictedError,
    ProtocolValue,
    UniqueConstraintError,
)
from kivyfn.store.schema.policy import ForeignKeyPolicy


if TYPE_CHECKING:
    from kivyfn.store.core.delta import Delta
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


class ConstraintGuard:
    """
    Enforces relational invariants against a Delta view.

    Validates:
        - uniqueness;
        - foreign-key existence;
        - restricted deletion.

    Does not mutate state.
    """

    def __init__(
        self,
        engine: Engine,
    ) -> None:
        self.engine = engine

    # ============================================================
    # Public API
    # ============================================================

    def validate(
        self,
        delta: Delta,
    ) -> None:
        self._validate_uniques(delta)
        self._validate_fk_existence(delta)
        self._validate_restrict(delta)

    # ============================================================
    # Unique constraints
    # ============================================================

    def _validate_uniques(
        self,
        delta: Delta,
    ) -> None:
        for node in delta.touched_nodes:
            candidate_ids = (
                delta.updated_ids(node)
                | delta.inserted_ids(node)
            )

            for unique_key in node.schema.unique_keys():
                # Only validate this unique constraint when at least one of
                # its fields was touched.
                if not set(unique_key) & delta.touched_fields(node):
                    continue

                seen: dict[Any, str] = {}

                for row_id in candidate_ids:
                    if not delta.exists(
                        node,
                        row_id,
                    ):
                        continue

                    value = self._build_unique_value(
                        delta,
                        node,
                        row_id,
                        unique_key,
                    )

                    if self._is_empty_unique_value(
                        value,
                    ):
                        continue

                    # Collision inside the same transaction.
                    existing_row_id = seen.get(
                        value,
                    )

                    if existing_row_id is not None:
                        raise UniqueConstraintError(
                            (
                                "Unique constraint violated on "
                                f"{node.schema.name}."
                                f"{'.'.join(unique_key)}"
                            ),
                            detail=ConstraintErrorDetail(
                                schema=node.schema.name,
                                fields=unique_key,
                                value=self._protocol_value(value),
                                row_id=row_id,
                                existing_row_id=existing_row_id,
                            ),
                        )

                    seen[value] = row_id

                    # Collision with the base state.
                    existing_row_id = node.lookup(
                        unique_key,
                        value,
                        first=True,
                    )

                    if (
                        not isinstance(existing_row_id, str)
                        or existing_row_id == row_id
                    ):
                        continue

                    if not delta.exists(
                        node,
                        existing_row_id,
                    ):
                        continue

                    existing_future_value = self._build_unique_value(
                        delta,
                        node,
                        existing_row_id,
                        unique_key,
                    )

                    if existing_future_value == value:
                        raise UniqueConstraintError(
                            (
                                "Unique constraint violated on "
                                f"{node.schema.name}."
                                f"{'.'.join(unique_key)}"
                            ),
                            detail=ConstraintErrorDetail(
                                schema=node.schema.name,
                                fields=unique_key,
                                value=self._protocol_value(value),
                                row_id=row_id,
                                existing_row_id=existing_row_id,
                            ),
                        )

    # ============================================================
    # Foreign-key existence
    # ============================================================

    def _validate_fk_existence(
        self,
        delta: Delta,
    ) -> None:
        for node in delta.touched_nodes:
            foreign_keys = self.engine.metadata.foreign_keys_of(
                node.schema.name,
            )

            if not foreign_keys:
                continue

            for foreign_key in foreign_keys:
                # Only validate a foreign key when its field was touched.
                if foreign_key.field not in delta.touched_fields(node):
                    continue

                target_node = self.engine.get_node_by_schema(
                    foreign_key.reference,
                )

                candidate_ids = (
                    delta.inserted_ids(node)
                    | delta.updated_ids(node)
                )

                for row_id in candidate_ids:
                    value = delta.value(
                        node,
                        row_id,
                        foreign_key.field,
                    )

                    # Null foreign keys are allowed.
                    if value is None:
                        continue

                    if not delta.exists(
                        target_node,
                        value,
                    ):
                        raise ForeignKeyNotFoundError(
                            (
                                "Foreign key constraint failed on "
                                f"{node.schema.name}."
                                f"{foreign_key.field} -> "
                                f"{foreign_key.reference}"
                            ),
                            detail=ConstraintErrorDetail(
                                schema=node.schema.name,
                                fields=(foreign_key.field,),
                                value=self._protocol_value(value),
                                row_id=row_id,
                                reference_schema=foreign_key.reference,
                                reference_field=foreign_key.field,
                            ),
                        )

    # ============================================================
    # Restricted deletion
    # ============================================================

    def _validate_restrict(
        self,
        delta: Delta,
    ) -> None:
        for target_node, removed_ids in delta._removed.items():
            foreign_keys = self.engine.metadata.referenced_by(
                target_node.schema.name,
            )

            for foreign_key in foreign_keys:
                if (
                    foreign_key.on_delete
                    is not ForeignKeyPolicy.RESTRICT
                ):
                    continue

                referencing_node = self.engine.get_node_by_schema(
                    foreign_key.schema,
                )

                for removed_id in removed_ids:
                    referencing_ids = referencing_node.lookup(
                        foreign_key.field,
                        removed_id,
                    )

                    if not isinstance(referencing_ids, set):
                        continue

                    for referencing_row_id in referencing_ids:
                        # A referencing row deleted in this
                        # transaction is safe.
                        if not delta.exists(
                            referencing_node,
                            referencing_row_id,
                        ):
                            continue

                        future_value = delta.value(
                            referencing_node,
                            referencing_row_id,
                            foreign_key.field,
                        )

                        if future_value == removed_id:
                            raise ForeignKeyRestrictedError(
                                (
                                    f"Cannot delete "
                                    f"{target_node.schema.name} "
                                    f"{removed_id!r} because it is "
                                    f"still referenced by "
                                    f"{foreign_key.schema}."
                                    f"{foreign_key.field}"
                                ),
                                detail=ConstraintErrorDetail(
                                    schema=target_node.schema.name,
                                    row_id=removed_id,
                                    referencing_schema=foreign_key.schema,
                                    referencing_field=foreign_key.field,
                                    referencing_row_ids=(
                                        referencing_row_id,
                                    ),
                                ),
                            )

    # ============================================================
    # Helpers
    # ============================================================

    def _build_unique_value(
        self,
        delta: Delta,
        node: Node,
        row_id: str,
        fields: tuple[str, ...],
    ) -> Any:
        if len(fields) == 1:
            return delta.value(
                node,
                row_id,
                fields[0],
            )

        return tuple(
            delta.value(
                node,
                row_id,
                field,
            )
            for field in fields
        )

    def _is_empty_unique_value(
        self,
        value: Any,
    ) -> bool:
        if value is None:
            return True

        if isinstance(value, tuple):
            return any(
                part is None
                for part in value
            )

        return False

    def _protocol_value(
        self,
        value: Any,
    ) -> ProtocolValue:
        if isinstance(value, tuple):
            return [
                self._protocol_value(part)
                for part in value
            ]

        return cast(
            ProtocolValue,
            value,
        )
