# src/kivyfn/store/query/expression.py
from __future__ import annotations

from collections.abc import Hashable, Iterable, Iterator
from dataclasses import dataclass
from enum import Enum


class ComparisonOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class MembershipOperator(str, Enum):
    IN = "in"
    NOT_IN = "not_in"


@dataclass(frozen=True, slots=True)
class FieldPath:
    parts: tuple[str, ...]

    def __init__(self, value: str | Iterable[str]) -> None:
        parts = (
            tuple(value.split(".")) if isinstance(value, str) else tuple(value)
        )

        if not parts:
            raise ValueError("Query field paths cannot be empty")

        if any(not isinstance(part, str) for part in parts):
            raise TypeError("Query field path parts must be strings")

        if any(not part for part in parts):
            raise ValueError("Query field path parts cannot be empty")

        object.__setattr__(self, "parts", parts)

    def __str__(self) -> str:
        return ".".join(self.parts)


class Expression:
    def __and__(self, other: Expression) -> Expression:
        return AndExpression.build(self, _require_expression(other))

    def __or__(self, other: Expression) -> Expression:
        return OrExpression.build(self, _require_expression(other))

    def __invert__(self) -> Expression:
        return NotExpression(self)

    def __bool__(self) -> bool:
        raise TypeError(
            "Query expressions cannot be converted to bool; "
            "use '&', '|', and '~' to combine them"
        )


@dataclass(frozen=True, slots=True)
class ComparisonExpression(Expression):
    field: FieldPath
    operator: ComparisonOperator
    value: Hashable

    def __post_init__(self) -> None:
        _require_hashable(self.value)


@dataclass(frozen=True, slots=True)
class MembershipExpression(Expression):
    field: FieldPath
    operator: MembershipOperator
    values: tuple[Hashable, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("Query membership values cannot be empty")

        for value in self.values:
            _require_hashable(value)


@dataclass(frozen=True, slots=True)
class AndExpression(Expression):
    expressions: tuple[Expression, ...]

    def __post_init__(self) -> None:
        _validate_logical_values(self.expressions)

    @classmethod
    def build(cls, *values: Expression) -> Expression:
        return _build_logical(cls, values, "AND")


@dataclass(frozen=True, slots=True)
class OrExpression(Expression):
    expressions: tuple[Expression, ...]

    def __post_init__(self) -> None:
        _validate_logical_values(self.expressions)

    @classmethod
    def build(cls, *values: Expression) -> Expression:
        return _build_logical(cls, values, "OR")


@dataclass(frozen=True, slots=True)
class NotExpression(Expression):
    expression: Expression

    def __post_init__(self) -> None:
        _require_expression(self.expression)


@dataclass(frozen=True, slots=True, eq=False)
class Field:
    path: FieldPath

    def __init__(self, value: str | Iterable[str]) -> None:
        object.__setattr__(self, "path", FieldPath(value))

    def __eq__(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, value: object
    ) -> ComparisonExpression:
        return self._compare(ComparisonOperator.EQ, value)

    def __ne__(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, value: object
    ) -> ComparisonExpression:
        return self._compare(ComparisonOperator.NE, value)

    def __gt__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.GT, value)

    def __ge__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.GTE, value)

    def __lt__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.LT, value)

    def __le__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.LTE, value)

    def in_(self, values: Iterable[Hashable]) -> MembershipExpression:
        return MembershipExpression(
            field=self.path,
            operator=MembershipOperator.IN,
            values=_membership_values(values),
        )

    def not_in(self, values: Iterable[Hashable]) -> MembershipExpression:
        return MembershipExpression(
            field=self.path,
            operator=MembershipOperator.NOT_IN,
            values=_membership_values(values),
        )

    def _compare(
        self, operator: ComparisonOperator, value: object
    ) -> ComparisonExpression:
        return ComparisonExpression(
            field=self.path, operator=operator, value=_require_hashable(value)
        )


def field(value: str | Iterable[str]) -> Field:
    return Field(value)


def iter_field_paths(expression: Expression) -> Iterator[FieldPath]:
    if isinstance(expression, ComparisonExpression):
        yield expression.field
        return

    if isinstance(expression, MembershipExpression):
        yield expression.field
        return

    if isinstance(expression, (AndExpression, OrExpression)):
        for child in expression.expressions:
            yield from iter_field_paths(child)

        return

    if isinstance(expression, NotExpression):
        yield from iter_field_paths(expression.expression)
        return

    raise TypeError(
        f"Unsupported query expression: {type(expression).__name__}"
    )


def _build_logical(
    expression_type: type[AndExpression] | type[OrExpression],
    values: tuple[Expression, ...],
    name: str,
) -> Expression:
    expressions: list[Expression] = []

    for value in values:
        expression = _require_expression(value)

        if isinstance(expression, expression_type):
            expressions.extend(expression.expressions)
        else:
            expressions.append(expression)

    if not expressions:
        raise ValueError(f"{name} expressions cannot be empty")

    if len(expressions) == 1:
        return expressions[0]

    return expression_type(tuple(expressions))


def _membership_values(values: Iterable[Hashable]) -> tuple[Hashable, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(
            "Query membership values must be a non-string iterable"
        )

    result = tuple(_require_hashable(value) for value in values)

    if not result:
        raise ValueError("Query membership values cannot be empty")

    return result


def _validate_logical_values(values: tuple[Expression, ...]) -> None:
    if not values:
        raise ValueError("Query logical expressions cannot be empty")

    for value in values:
        _require_expression(value)


def _require_expression(value: object) -> Expression:
    if not isinstance(value, Expression):
        raise TypeError("Query logical operands must be query expressions")

    return value


def _require_hashable(value: object) -> Hashable:
    if not isinstance(value, Hashable):
        raise TypeError("Query comparison values must be hashable")

    return value
