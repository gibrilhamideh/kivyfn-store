# src/kivyfn/store/core/constraintguard.py
from __future__ import annotations

from typing import TYPE_CHECKING

from kivyfn.store.core.ensure import EnsurePolicy
from kivyfn.store.error import (
    ConstraintErrorDetail,
    ForeignKeyNotFoundError,
    ForeignKeyRestrictedError,
)
from kivyfn.store.schema import SingleSchema

if TYPE_CHECKING:
    from kivyfn.store.core.delta import Delta
    from kivyfn.store.core.engine import Engine


class ConstraintGuard:
    """Validate relationships against final transaction values.

    Field validation belongs to Compiler; uniqueness is checked when Nodes
    prepare their final indexes. Neither operation mutates canonical data.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def validate(self, delta: Delta) -> None:
        self._validate_restrict(delta)
        self._validate_foreign_keys(delta)
        EnsurePolicy(self.engine).validate(delta)

    def _validate_foreign_keys(self, delta: Delta) -> None:
        for node in delta.touched_nodes:
            data = delta.data(node)
            if isinstance(node.schema, SingleSchema):
                rows = {None: data}
            else:
                candidates = delta.inserted_ids(node) | delta.updated_ids(node)
                rows = {key: data[key] for key in candidates if key in data}

            for foreign_key in self.engine.metadata.foreign_keys_of(
                node.schema.name
            ):
                if foreign_key.field not in delta.touched_fields(node):
                    continue
                target = self.engine.get_node_by_schema(foreign_key.reference)
                for row_id, row in rows.items():
                    value = row.get(foreign_key.field)
                    if value is None or delta.exists(target, value):
                        continue
                    raise ForeignKeyNotFoundError(
                        f"Foreign key {node.schema.name}.{foreign_key.field} "
                        f"references missing {foreign_key.reference}"
                        f"[{value!r}]",
                        detail=ConstraintErrorDetail(
                            schema=node.schema.name,
                            fields=(foreign_key.field,),
                            value=value,
                            row_id=row_id,
                            reference_schema=foreign_key.reference,
                            reference_field=foreign_key.field,
                        ),
                    )

    def _validate_restrict(self, delta: Delta) -> None:
        for target in delta.removed_nodes:
            for foreign_key in self.engine.metadata.referenced_by(
                target.schema.name
            ):
                node = self.engine.get_node_by_schema(foreign_key.schema)
                data = delta.data(node)
                rows = (
                    {None: data}
                    if isinstance(node.schema, SingleSchema)
                    else data
                )
                for removed_id in delta.removed_ids(target):
                    for row_id, row in rows.items():
                        if row.get(foreign_key.field) != removed_id:
                            continue
                        raise ForeignKeyRestrictedError(
                            f"Cannot delete {target.schema.name}"
                            f"[{removed_id!r}]: "
                            f"referenced by {node.schema.name}."
                            f"{foreign_key.field}",
                            detail=ConstraintErrorDetail(
                                schema=target.schema.name,
                                row_id=removed_id,
                                referencing_schema=node.schema.name,
                                referencing_field=foreign_key.field,
                                referencing_row_ids=(
                                    () if row_id is None else (row_id,)
                                ),
                            ),
                        )
