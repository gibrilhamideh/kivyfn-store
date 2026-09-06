from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kivyfn.store.address import Address
from kivyfn.store.query.expression import (
    AndExpression,
    ComparisonExpression,
    ComparisonOperator,
    Expression,
    FieldPath,
    MembershipExpression,
    MembershipOperator,
    NotExpression,
    OrExpression,
    iter_field_paths,
)
from kivyfn.store.query.query import JoinKind, Query
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.fields import ForeignKeyField


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node
    from kivyfn.store.schema import Schema


_MISSING = object()


@dataclass(frozen=True, slots=True)
class CompiledJoin:
    query_path: tuple[str, ...]
    owner_path: tuple[str, ...]
    field_name: str
    output_name: str
    kind: JoinKind
    target: Node


@dataclass(frozen=True, slots=True)
class QueryPlan:
    query: Query[Any]
    source: Node
    joins: tuple[CompiledJoin, ...]
    dependencies: frozenset[Address[Any]]

    @classmethod
    def compile(
        cls,
        engine: Engine,
        query: Query[Any],
    ) -> QueryPlan:
        source = _resolve_source(
            engine,
            query,
        )

        if not isinstance(
            source.schema,
            ManySchema,
        ):
            raise ValueError(
                "Query sources must use a ManySchema"
            )

        joins: list[CompiledJoin] = []
        dependencies: set[Address[Any]] = {
            source.address,
        }

        material_paths: dict[
            tuple[str, ...],
            Node,
        ] = {
            (): source,
        }

        query_paths: dict[
            tuple[str, ...],
            tuple[str, ...],
        ] = {
            (): (),
        }

        for join in query.joins:
            requested = join.path.parts
            owner_requested = requested[:-1]

            owner_path = query_paths.get(
                owner_requested,
                owner_requested,
            )

            owner = material_paths.get(
                owner_path,
            )

            if owner is None:
                raise ValueError(
                    (
                        "Query joins must declare their "
                        f"parent path first: {join.path!s}"
                    )
                )

            field_name, relation = _foreign_key(
                owner.schema,
                requested[-1],
            )

            target = engine.get_node_by_schema(
                relation.schema,
            )

            if not isinstance(
                target.schema,
                ManySchema,
            ):
                raise ValueError(
                    (
                        "Query foreign-key targets must "
                        "use a ManySchema"
                    )
                )

            output_name = (
                join.alias
                or relation.relation_name(
                    field_name,
                )
            )

            if output_name == field_name:
                raise ValueError(
                    (
                        f"Foreign-key field {field_name!r} on "
                        f"{owner.schema.name} needs an '_id' "
                        "suffix, a relation name, or an "
                        "explicit query alias"
                    )
                )

            material_path = (
                *owner_path,
                output_name,
            )

            if material_path in material_paths:
                raise ValueError(
                    (
                        "Query join output path already exists: "
                        f"{'.'.join(material_path)}"
                    )
                )

            compiled = CompiledJoin(
                query_path=requested,
                owner_path=owner_path,
                field_name=field_name,
                output_name=output_name,
                kind=join.kind,
                target=target,
            )

            joins.append(
                compiled,
            )

            material_paths[
                material_path
            ] = target

            query_paths[
                requested
            ] = material_path

            dependencies.add(
                target.address,
            )

        if query.predicate is not None:
            for path in iter_field_paths(
                query.predicate,
            ):
                _validate_filter_path(
                    path,
                    source,
                    material_paths,
                )

        return cls(
            query=query,
            source=source,
            joins=tuple(joins),
            dependencies=frozenset(
                dependencies,
            ),
        )

    def execute(
        self,
    ) -> dict[str, dict[str, Any]]:
        source_data = self.source.snapshot()

        if not isinstance(
            source_data,
            dict,
        ):
            raise TypeError(
                "Query source snapshot must be a mapping"
            )

        result: dict[
            str,
            dict[str, Any],
        ] = {}

        for row_id, source_row in source_data.items():
            if not isinstance(
                source_row,
                dict,
            ):
                raise TypeError(
                    "Query source rows must be mappings"
                )

            materialized = dict(
                source_row,
            )

            if not self._apply_joins(
                materialized,
            ):
                continue

            predicate = self.query.predicate

            if predicate is not None:
                if not evaluate_expression(
                    predicate,
                    materialized,
                ):
                    continue

            result[
                str(row_id)
            ] = materialized

        return result

    def _apply_joins(
        self,
        materialized: dict[str, Any],
    ) -> bool:
        for join in self.joins:
            owner = _resolve_mapping(
                materialized,
                join.owner_path,
            )

            foreign_id = (
                owner.get(
                    join.field_name,
                )
                if owner is not None
                else None
            )

            row = (
                join.target.snapshot(
                    id=str(foreign_id),
                )
                if foreign_id is not None
                else None
            )

            if not isinstance(
                row,
                dict,
            ):
                if join.kind is JoinKind.INNER:
                    return False

                if owner is not None:
                    owner[
                        join.output_name
                    ] = None

                continue

            if owner is not None:
                owner[
                    join.output_name
                ] = dict(row)

        return True


def evaluate_expression(
    expression: Expression,
    row: Mapping[str, Any],
) -> bool:
    if isinstance(
        expression,
        ComparisonExpression,
    ):
        value = _resolve_path(
            row,
            expression.field,
        )

        if value is _MISSING:
            return False

        return _compare(
            value,
            expression.operator,
            expression.value,
        )

    if isinstance(
        expression,
        MembershipExpression,
    ):
        value = _resolve_path(
            row,
            expression.field,
        )

        if value is _MISSING:
            return False

        contained = (
            value
            in expression.values
        )

        if (
            expression.operator
            is MembershipOperator.IN
        ):
            return contained

        return not contained

    if isinstance(
        expression,
        AndExpression,
    ):
        return all(
            evaluate_expression(
                child,
                row,
            )
            for child
            in expression.expressions
        )

    if isinstance(
        expression,
        OrExpression,
    ):
        return any(
            evaluate_expression(
                child,
                row,
            )
            for child
            in expression.expressions
        )

    if isinstance(
        expression,
        NotExpression,
    ):
        return not evaluate_expression(
            expression.expression,
            row,
        )

    raise TypeError(
        (
            "Unsupported query expression: "
            f"{type(expression).__name__}"
        )
    )


def _compare(
    left: Any,
    operator: ComparisonOperator,
    right: Any,
) -> bool:
    if operator is ComparisonOperator.EQ:
        return left == right

    if operator is ComparisonOperator.NE:
        return left != right

    if operator is ComparisonOperator.GT:
        return left > right

    if operator is ComparisonOperator.GTE:
        return left >= right

    if operator is ComparisonOperator.LT:
        return left < right

    return left <= right


def _resolve_path(
    row: Mapping[str, Any],
    path: FieldPath,
) -> Any:
    current: Any = row

    for part in path.parts:
        if current is None:
            return None

        if not isinstance(
            current,
            Mapping,
        ):
            return _MISSING

        if part not in current:
            return _MISSING

        current = current[
            part
        ]

    return current


def _resolve_mapping(
    row: dict[str, Any],
    path: tuple[str, ...],
) -> dict[str, Any] | None:
    current: Any = row

    for part in path:
        if not isinstance(
            current,
            dict,
        ):
            return None

        current = current.get(
            part,
        )

    if not isinstance(
        current,
        dict,
    ):
        return None

    return current


def _resolve_source(
    engine: Engine,
    query: Query[Any],
) -> Node:
    source = query.source

    if isinstance(
        source,
        Address,
    ):
        node = engine.get_node_by_address(
            source,
        )
    else:
        schema_name = query.schema_name

        if schema_name is None:
            raise TypeError(
                "Query source cannot be resolved"
            )

        node = engine.get_node_by_schema(
            schema_name,
        )

    if not isinstance(
        node.schema,
        ManySchema,
    ):
        raise ValueError(
            "Query source must use a ManySchema"
        )

    return node


def _foreign_key(
    schema: Schema,
    relation_name: str,
) -> tuple[
    str,
    ForeignKeyField,
]:
    candidates: list[
        tuple[str, ForeignKeyField]
    ] = []

    for (
        field_name,
        field,
    ) in schema.foreign_key_fields().items():
        if field_name == relation_name:
            candidates.append(
                (
                    field_name,
                    field,
                )
            )
            continue

        if (
            field.relation_name(
                field_name,
            )
            == relation_name
        ):
            candidates.append(
                (
                    field_name,
                    field,
                )
            )

    if not candidates:
        raise ValueError(
            (
                f"Query join relation {relation_name!r} "
                f"does not exist on {schema.name}"
            )
        )

    if len(candidates) > 1:
        raise ValueError(
            (
                f"Query join relation {relation_name!r} "
                f"is ambiguous on {schema.name}"
            )
        )

    return candidates[0]


def _validate_filter_path(
    path: FieldPath,
    source: Node,
    joined: Mapping[
        tuple[str, ...],
        Node,
    ],
) -> None:
    parts = path.parts

    if len(parts) == 1:
        source.schema.get_field(
            parts[0],
        )
        return

    owner_path = parts[:-1]
    owner = joined.get(
        owner_path,
    )

    if owner is None:
        raise ValueError(
            (
                f"Query field path {path!s} "
                "is not joined"
            )
        )

    owner.schema.get_field(
        parts[-1],
    )
