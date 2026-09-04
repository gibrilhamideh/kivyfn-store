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
from kivyfn.store.schema.fields import ForeignKeyField


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


_MISSING = object()


@dataclass(frozen=True, slots=True)
class CompiledJoin:
    owner_alias: str | None
    field_name: str
    alias: str
    kind: JoinKind
    target: Node


@dataclass(frozen=True, slots=True)
class QueryPlan:
    query: Query
    source: Node
    joins: tuple[CompiledJoin, ...]
    dependencies: frozenset[Address[Any]]

    @classmethod
    def compile(cls, engine: Engine, query: Query) -> QueryPlan:
        source = engine.get_node_by_address(query.source)

        if not source.schema.many:
            raise ValueError(
                "Query sources must use a many=True schema"
            )

        aliases: dict[str, Node] = {}
        joins: list[CompiledJoin] = []
        dependencies: set[Address[Any]] = {
            source.address,
        }

        for join in query.joins:
            owner_alias, field_name = _join_owner(
                join.path,
                aliases,
            )
            owner = (
                source
                if owner_alias is None
                else aliases[owner_alias]
            )

            if join.alias in source.schema.field_names():
                raise ValueError(
                    f"Query join alias {join.alias!r} conflicts with a "
                    "source field"
                )

            field = owner.schema.get_field(field_name)

            if not isinstance(field, ForeignKeyField):
                raise ValueError(
                    f"Query join path {join.path!s} is not a foreign key"
                )

            target = engine.get_node_by_schema(field.schema)

            if not target.schema.many:
                raise ValueError(
                    "Query foreign-key targets must use many=True schemas"
                )

            aliases[join.alias] = target
            dependencies.add(target.address)
            joins.append(
                CompiledJoin(
                    owner_alias=owner_alias,
                    field_name=field_name,
                    alias=join.alias,
                    kind=join.kind,
                    target=target,
                )
            )

        if query.predicate is not None:
            for path in iter_field_paths(query.predicate):
                _validate_filter_path(
                    path,
                    source,
                    aliases,
                )

        return cls(
            query=query,
            source=source,
            joins=tuple(joins),
            dependencies=frozenset(dependencies),
        )

    def execute(self) -> dict[str, dict[str, Any]]:
        source_data = self.source.snapshot()

        if not isinstance(source_data, dict):
            raise TypeError(
                "Query source snapshot must be a mapping"
            )

        result: dict[str, dict[str, Any]] = {}

        for row_id, source_row in source_data.items():
            if not isinstance(source_row, dict):
                raise TypeError(
                    "Query source rows must be mappings"
                )

            materialized = dict(source_row)
            joined_rows: dict[str, dict[str, Any] | None] = {}

            if not self._apply_joins(materialized, joined_rows):
                continue

            predicate = self.query.predicate

            if predicate is not None:
                if not evaluate_expression(predicate, materialized):
                    continue

            result[str(row_id)] = materialized

        return result

    def _apply_joins(
        self,
        materialized: dict[str, Any],
        joined_rows: dict[str, dict[str, Any] | None],
    ) -> bool:
        for join in self.joins:
            owner = (
                materialized
                if join.owner_alias is None
                else joined_rows.get(join.owner_alias)
            )
            foreign_id = (
                owner.get(join.field_name)
                if isinstance(owner, dict)
                else None
            )
            row = (
                join.target.snapshot(id=str(foreign_id))
                if foreign_id is not None
                else None
            )

            if not isinstance(row, dict):
                if join.kind is JoinKind.INNER:
                    return False

                joined_rows[join.alias] = None
                materialized[join.alias] = None
                continue

            copied = dict(row)
            joined_rows[join.alias] = copied
            materialized[join.alias] = copied

        return True


def evaluate_expression(
    expression: Expression,
    row: Mapping[str, Any],
) -> bool:
    if isinstance(expression, ComparisonExpression):
        value = _resolve_path(row, expression.field)

        if value is _MISSING:
            return False

        return _compare(
            value,
            expression.operator,
            expression.value,
        )

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
            evaluate_expression(child, row)
            for child in expression.expressions
        )

    if isinstance(expression, OrExpression):
        return any(
            evaluate_expression(child, row)
            for child in expression.expressions
        )

    if isinstance(expression, NotExpression):
        return not evaluate_expression(
            expression.expression,
            row,
        )

    raise TypeError(
        f"Unsupported query expression: {type(expression).__name__}"
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

        if not isinstance(current, Mapping):
            return _MISSING

        if part not in current:
            return _MISSING

        current = current[part]

    return current


def _join_owner(
    path: FieldPath,
    aliases: Mapping[str, Node],
) -> tuple[str | None, str]:
    if len(path.parts) == 1:
        return None, path.parts[0]

    if len(path.parts) != 2:
        raise ValueError(
            "Query join paths must be either 'field' or 'alias.field'"
        )

    owner_alias, field_name = path.parts

    if owner_alias not in aliases:
        raise ValueError(
            f"Query join refers to unknown alias {owner_alias!r}"
        )

    return owner_alias, field_name


def _validate_filter_path(
    path: FieldPath,
    source: Node,
    aliases: Mapping[str, Node],
) -> None:
    first = path.parts[0]

    if first in aliases:
        if len(path.parts) == 1:
            return

        if len(path.parts) != 2:
            raise ValueError(
                "Joined query fields must use 'alias.field' paths"
            )

        aliases[first].schema.get_field(path.parts[1])
        return

    if len(path.parts) != 1:
        raise ValueError(
            f"Query field path {path!s} uses an unknown alias"
        )

    source.schema.get_field(first)
