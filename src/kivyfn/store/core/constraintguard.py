# src/kivyfn/store/core/constraintguard.py
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
    """Validate relational constraints against a transaction Delta."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def validate(self, delta: Delta) -> None:
        self._validate_uniques(delta)
        self._validate_foreign_keys(delta)
        self._validate_restrict(delta)

    # -------------------------------------------------
    # Unique constraints
    # -------------------------------------------------

    def _validate_uniques(self, delta: Delta) -> None:
        for node in delta.touched_nodes:
            candidate_ids = delta.updated_ids(node) | delta.inserted_ids(node)
            touched_fields = delta.touched_fields(node)

            for unique_key in node.schema.unique_keys():
                if not (set(unique_key) & touched_fields):
                    continue

                seen: dict[Any, str] = {}

                for row_id in candidate_ids:
                    if not delta.exists(node, row_id):
                        continue

                    value = self._unique_value(delta, node, row_id, unique_key)

                    if self._empty_unique_value(value):
                        continue

                    transaction_row_id = seen.get(value)

                    if transaction_row_id is not None:
                        self._raise_unique(
                            node,
                            unique_key,
                            value,
                            row_id,
                            transaction_row_id,
                        )

                    seen[value] = row_id

                    existing_row_id = node.lookup_one(unique_key, value)

                    if (
                        not isinstance(existing_row_id, str)
                        or existing_row_id == row_id
                    ):
                        continue

                    if not delta.exists(node, existing_row_id):
                        continue

                    existing_value = self._unique_value(
                        delta,
                        node,
                        existing_row_id,
                        unique_key,
                    )

                    if existing_value == value:
                        self._raise_unique(
                            node,
                            unique_key,
                            value,
                            row_id,
                            existing_row_id,
                        )

    def _raise_unique(
        self,
        node: Node,
        fields: tuple[str, ...],
        value: Any,
        row_id: str,
        existing_row_id: str,
    ) -> None:
        raise UniqueConstraintError(
            f"Unique constraint violated on {node.schema.name}.{'.'.join(fields)}",
            detail=ConstraintErrorDetail(
                schema=node.schema.name,
                fields=fields,
                value=self._protocol_value(value),
                row_id=row_id,
                existing_row_id=existing_row_id,
            ),
        )

    # -------------------------------------------------
    # Foreign-key existence
    # -------------------------------------------------

    def _validate_foreign_keys(self, delta: Delta) -> None:
        for node in delta.touched_nodes:
            foreign_keys = self.engine.metadata.foreign_keys_of(
                node.schema.name
            )

            if not foreign_keys:
                continue

            candidate_ids = delta.inserted_ids(node) | delta.updated_ids(node)
            touched_fields = delta.touched_fields(node)

            for foreign_key in foreign_keys:
                if foreign_key.field not in touched_fields:
                    continue

                target_node = self.engine.get_node_by_schema(
                    foreign_key.reference
                )

                for row_id in candidate_ids:
                    value = delta.value(node, row_id, foreign_key.field)

                    if value is None:
                        continue

                    reference_id = cast(str, value)

                    if delta.exists(target_node, reference_id):
                        continue

                    raise ForeignKeyNotFoundError(
                        (
                            f"Foreign key constraint failed on {node.schema.name}."
                            f"{foreign_key.field} -> {foreign_key.reference}"
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

    # -------------------------------------------------
    # Restricted deletion
    # -------------------------------------------------

    def _validate_restrict(self, delta: Delta) -> None:
        for target_node in delta.removed_nodes:
            foreign_keys = self.engine.metadata.referenced_by(
                target_node.schema.name
            )

            restricted = (
                foreign_key
                for foreign_key in foreign_keys
                if foreign_key.on_delete is ForeignKeyPolicy.RESTRICT
            )

            for foreign_key in restricted:
                referencing_node = self.engine.get_node_by_schema(
                    foreign_key.schema
                )

                for removed_id in delta.removed_ids(target_node):
                    referencing_ids = referencing_node.lookup_all(
                        foreign_key.field,
                        removed_id,
                    )

                    for referencing_row_id in referencing_ids:
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

                        if future_value != removed_id:
                            continue

                        raise ForeignKeyRestrictedError(
                            (
                                f"Cannot delete {target_node.schema.name} "
                                f"{removed_id!r} because it is still referenced by "
                                f"{foreign_key.schema}.{foreign_key.field}"
                            ),
                            detail=ConstraintErrorDetail(
                                schema=target_node.schema.name,
                                row_id=removed_id,
                                referencing_schema=foreign_key.schema,
                                referencing_field=foreign_key.field,
                                referencing_row_ids=(referencing_row_id,),
                            ),
                        )

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------

    def _unique_value(
        self,
        delta: Delta,
        node: Node,
        row_id: str,
        fields: tuple[str, ...],
    ) -> Any:
        if len(fields) == 1:
            return delta.value(node, row_id, fields[0])

        return tuple(delta.value(node, row_id, field) for field in fields)

    def _empty_unique_value(self, value: Any) -> bool:
        if value is None:
            return True

        if isinstance(value, tuple):
            return any(part is None for part in value)

        return False

    def _protocol_value(self, value: Any) -> ProtocolValue:
        if isinstance(value, tuple):
            return [self._protocol_value(part) for part in value]

        return cast(ProtocolValue, value)

