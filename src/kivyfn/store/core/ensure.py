from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from kivyfn.store.core.instruction import InsertInstruction
from kivyfn.store.error import ConstraintErrorDetail, ForeignKeyRestrictedError
from kivyfn.store.operations import OperationSource
from kivyfn.store.query.plan import evaluate_expression
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store.core.delta import Delta
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.core.metadata import ForeignKey


class EnsurePolicy:
    """Fill required relationships without overwriting existing children."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def missing(
        self, delta: Delta | None = None
    ) -> Iterator[tuple[ForeignKey, str]]:
        for relation in self.engine.metadata.ensured_foreign_keys:
            child = self.engine.get_node_by_schema(relation.schema)
            parent = self.engine.get_node_by_schema(relation.reference)
            parents = parent.value if delta is None else delta.data(parent)
            children = child.value if delta is None else delta.data(child)

            if delta is None:
                candidates = set(parents)
            else:
                candidates = set(delta.inserted_ids(parent))
                if relation.ensure_where is not None:
                    candidates.update(delta.updated_ids(parent))
                changed = set(delta.removed_ids(child))
                if relation.field in delta.touched_fields(child):
                    changed.update(delta.updated_ids(child))
                for row_id in changed:
                    previous = child.value.get(row_id)
                    if previous is not None:
                        reference = previous.get(relation.field)
                        if reference is not None:
                            candidates.add(reference)

            if not candidates:
                continue
            linked = {row[relation.field] for row in children.values()}
            for parent_id in sorted(candidates):
                row = parents.get(parent_id)
                if row is None or parent_id in linked:
                    continue
                predicate = relation.ensure_where
                if predicate is None or evaluate_expression(predicate, row):
                    yield relation, parent_id

    def next_insert(self, delta: Delta) -> InsertInstruction | None:
        """Choose one insertion, then let its triggers finish before retry."""
        for relation, parent_id in self.missing(delta):
            child = self.engine.get_node_by_schema(relation.schema)
            parent = self.engine.get_node_by_schema(relation.reference)
            existing = child.lookup_one(relation.field, parent_id)
            if existing is not None and parent_id not in delta.inserted_ids(
                parent
            ):
                # Do not reset removed settings. Another generated insert's
                # trigger may supply a replacement before final validation.
                continue
            return InsertInstruction(
                address=child.address,
                id=generate_uuid(),
                value={relation.field: parent_id},
                source=OperationSource.POLICY,
            )
        return None

    def validate(self, delta: Delta) -> None:
        missing = next(self.missing(delta), None)
        if missing is None:
            return
        relation, parent_id = missing
        raise ForeignKeyRestrictedError(
            f"{relation.reference}[{parent_id!r}] requires one "
            f"{relation.schema} row through {relation.field}",
            detail=ConstraintErrorDetail(
                schema=relation.schema,
                fields=(relation.field,),
                value=parent_id,
                reference_schema=relation.reference,
                reference_field=relation.field,
            ),
        )

    def boot(self) -> None:
        """Build the baseline in dependency order, without triggers."""
        for relation, parent_id in self.missing():
            child = self.engine.get_node_by_schema(relation.schema)
            row_id = generate_uuid()
            row = child.build_row({relation.field: parent_id}, row_id=row_id)
            child.insert(row_id, row)
