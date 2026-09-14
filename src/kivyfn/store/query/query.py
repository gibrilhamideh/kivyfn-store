# src/kivyfn/store/query/query.py
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Generic, TypeVar

from kivyfn.store.address import Address
from kivyfn.store.query.expression import AndExpression, Expression, FieldPath


if TYPE_CHECKING:
    from kivyfn.store.schema import ManySchema


SchemaT = TypeVar("SchemaT", bound="ManySchema")


class JoinKind(str, Enum):
    INNER = "inner"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class JoinSpec:
    path: FieldPath
    kind: JoinKind
    alias: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, FieldPath):
            raise TypeError("Query join path must be a FieldPath")

        if not isinstance(self.kind, JoinKind):
            raise TypeError("Query join kind must be a JoinKind")

        _validate_alias(self.alias)


@dataclass(frozen=True, slots=True)
class Query(Generic[SchemaT]):
    """
    Immutable relational Query description.

    Query execution and live-State compatibility are separate concerns.
    """

    source: Address | type[SchemaT] | str
    joins: tuple[JoinSpec, ...] = ()
    predicate: Expression | None = None

    def __post_init__(self) -> None:
        if not _valid_source(self.source):
            raise TypeError(
                "Query source must be an Address, "
                "ManySchema class, or schema name"
            )

        joins = tuple(self.joins)

        for join in joins:
            if not isinstance(join, JoinSpec):
                raise TypeError("Query joins must be JoinSpec values")

        _validate_joins(joins)

        if self.predicate is not None and not isinstance(
            self.predicate, Expression
        ):
            raise TypeError("Query predicate must be a query expression")

        object.__setattr__(self, "joins", joins)

    def where(self, *expressions: Expression) -> Query[SchemaT]:
        if not expressions:
            raise ValueError("Query.where() requires at least one expression")

        predicate = AndExpression.build(
            *(_require_expression(expression) for expression in expressions)
        )

        if self.predicate is not None:
            predicate = AndExpression.build(self.predicate, predicate)

        return replace(self, predicate=predicate)

    def join(self, path: str | FieldPath, *, as_: str) -> Query[SchemaT]:
        return self._join(path, alias=as_, kind=JoinKind.INNER)

    def left_join(self, path: str | FieldPath, *, as_: str) -> Query[SchemaT]:
        return self._join(path, alias=as_, kind=JoinKind.LEFT)

    @property
    def schema_name(self) -> str | None:
        source = self.source

        if isinstance(source, str):
            return source

        if isinstance(source, type):
            return source.__name__

        return None

    def _join(
        self, path: str | FieldPath, *, alias: str, kind: JoinKind
    ) -> Query[SchemaT]:
        field_path = path if isinstance(path, FieldPath) else FieldPath(path)

        join = JoinSpec(path=field_path, kind=kind, alias=alias)

        return replace(self, joins=(*self.joins, join))


def _valid_source(source: object) -> bool:
    if isinstance(source, Address):
        return True

    if isinstance(source, str):
        return bool(source)

    if not isinstance(source, type):
        return False

    from kivyfn.store.schema import ManySchema

    return issubclass(source, ManySchema)


def _validate_joins(joins: tuple[JoinSpec, ...]) -> None:
    paths: set[FieldPath] = set()
    aliases: set[str] = set()

    for join in joins:
        if join.path in paths:
            raise ValueError(f"Query join path {join.path!s} is already used")

        paths.add(join.path)

        if join.alias in aliases:
            raise ValueError(
                f"Query join alias {join.alias!r} is already used"
            )

        aliases.add(join.alias)


def _validate_alias(alias: str) -> None:
    if not isinstance(alias, str) or not alias:
        raise ValueError("Query join aliases must be non-empty strings")

    if "." in alias:
        raise ValueError("Query join aliases cannot contain dots")


def _require_expression(value: object) -> Expression:
    if not isinstance(value, Expression):
        raise TypeError("Query.where() arguments must be query expressions")

    return value
