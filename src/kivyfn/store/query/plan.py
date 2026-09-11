# src/kivyfn/store/query/plan.py
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
from kivyfn.store.query.result import QueryResult, QueryRow
from kivyfn.store.query.shape import QueryJoinShape, QueryShape
from kivyfn.store.schema import ManySchema


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node
    from kivyfn.store.schema import Schema


_MISSING = object()


@dataclass(frozen=True, slots=True)
class CompiledJoin:
    """One Query join resolved to its target Store node."""

    shape: QueryJoinShape
    target: Node


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """Executable Store plan for one immutable Query."""

    query: Query[Any]
    shape: QueryShape
    source: Node
    joins: tuple[CompiledJoin, ...]
    dependencies: frozenset[Address]

    @classmethod
    def compile(cls, engine: Engine, query: Query[Any]) -> QueryPlan:
        source = _resolve_source(engine, query)

        source_schema = source.schema

        if not isinstance(source_schema, ManySchema):
            raise ValueError("Query source must use a ManySchema")

        shape = QueryShape.build(
            query,
            source_schema,
            lambda name: engine.get_node_by_schema(name).schema,
        )

        joins = tuple(
            CompiledJoin(
                shape=join,
                target=engine.get_node_by_schema(join.target_schema),
            )
            for join in shape.joins
        )

        relations: dict[tuple[str, ...], Schema] = {(): source.schema}

        for join in joins:
            relations[join.shape.relation_path] = join.target.schema

        if query.predicate is not None:
            for path in iter_field_paths(query.predicate):
                _validate_filter_path(path, relations)

        dependencies = frozenset(
            {source.address, *(join.target.address for join in joins)}
        )

        return cls(
            query=query,
            shape=shape,
            source=source,
            joins=joins,
            dependencies=dependencies,
        )

    @property
    def live_compatible(self) -> bool:
        return self.shape.live_compatible

    def execute(self) -> QueryResult:
        source_data = self.source.snapshot()

        if not isinstance(source_data, Mapping):
            raise TypeError("Query source snapshot must be a mapping")

        result: list[QueryRow] = []

        for row_id, source_row in source_data.items():
            if not isinstance(source_row, Mapping):
                raise TypeError("Query source rows must be mappings")

            context = dict(source_row)
            output = dict(source_row)

            if not self._apply_joins(context, output):
                continue

            predicate = self.query.predicate

            if predicate is not None and not evaluate_expression(
                predicate, context
            ):
                continue

            result.append(QueryRow(id=str(row_id), values=output))

        return tuple(result)

    def _apply_joins(
        self, context: dict[str, Any], output: dict[str, Any]
    ) -> bool:
        for join in self.joins:
            shape = join.shape

            owner = _resolve_mapping(context, shape.owner_path)

            if owner is None:
                if shape.kind is JoinKind.INNER:
                    return False

                _write_join_output(output, join, None)
                continue

            foreign_id = owner.get(shape.field_name)

            row = (
                join.target.snapshot(id=str(foreign_id))
                if foreign_id is not None
                else None
            )

            if not isinstance(row, Mapping):
                if shape.kind is JoinKind.INNER:
                    return False

                owner[shape.output_name] = None

                _write_join_output(output, join, None)
                continue

            joined_row = dict(row)

            owner[shape.output_name] = joined_row

            _write_join_output(output, join, joined_row)

        return True


def evaluate_expression(
    expression: Expression, row: Mapping[str, Any]
) -> bool:
    if isinstance(expression, ComparisonExpression):
        value = _resolve_path(row, expression.field)

        if value is _MISSING:
            return False

        return _compare(value, expression.operator, expression.value)

    if isinstance(expression, MembershipExpression):
        value = _resolve_path(row, expression.field)

        if value is _MISSING:
            return False

        contained = value in expression.values

        if expression.operator is MembershipOperator.IN:
            return contained

        return not contained

    if isinstance(expression, AndExpression):
        return all(
            evaluate_expression(child, row) for child in expression.expressions
        )

    if isinstance(expression, OrExpression):
        return any(
            evaluate_expression(child, row) for child in expression.expressions
        )

    if isinstance(expression, NotExpression):
        return not evaluate_expression(expression.expression, row)

    raise TypeError(
        f"Unsupported query expression: {type(expression).__name__}"
    )


def _write_join_output(
    output: dict[str, Any], join: CompiledJoin, row: Mapping[str, Any] | None
) -> None:
    shape = join.shape

    for field_name in join.target.schema.fields():
        output_name = f"{shape.output_prefix}_{field_name}"

        output[output_name] = row.get(field_name) if row is not None else None


def _compare(left: Any, operator: ComparisonOperator, right: Any) -> bool:
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

    if operator is ComparisonOperator.LTE:
        return left <= right

    raise ValueError(f"Unsupported comparison operator: {operator!r}")


def _resolve_path(row: Mapping[str, Any], path: FieldPath) -> Any:
    current: Any = row

    for part in path.parts:
        if current is None:
            return None

        if not isinstance(current, Mapping):
            return _MISSING

        if part not in current:
            return _MISSING

        current = current[part]

    return current


def _resolve_mapping(
    row: dict[str, Any], path: tuple[str, ...]
) -> dict[str, Any] | None:
    current: Any = row

    for part in path:
        if not isinstance(current, dict):
            return None

        current = current.get(part)

    if not isinstance(current, dict):
        return None

    return current


def _resolve_source(engine: Engine, query: Query[Any]) -> Node:
    source = query.source

    if isinstance(source, Address):
        node = engine.get_node_by_address(source)
    else:
        schema_name = query.schema_name

        if schema_name is None:
            raise TypeError("Query source cannot be resolved")

        node = engine.get_node_by_schema(schema_name)

    if not isinstance(node.schema, ManySchema):
        raise ValueError("Query source must use a ManySchema")

    return node


def _validate_filter_path(
    path: FieldPath, relations: Mapping[tuple[str, ...], Schema]
) -> None:
    parts = path.parts

    if len(parts) == 1:
        relations[()].get_field(parts[0])
        return

    owner = relations.get(parts[:-1])

    if owner is None:
        raise ValueError(f"Query field path {path!s} is not joined")

    owner.get_field(parts[-1])
