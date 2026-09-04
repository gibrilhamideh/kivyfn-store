from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Iterator


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
        if isinstance(value, str):
            parts = tuple(value.split("."))
        else:
            parts = tuple(value)

        if not parts or any(not part for part in parts):
            raise ValueError(
                "Query field paths must contain non-empty parts"
            )

        if any(not isinstance(part, str) for part in parts):
            raise TypeError(
                "Query field path parts must be strings"
            )

        object.__setattr__(self, "parts", parts)

    def __str__(self) -> str:
        return ".".join(self.parts)

    def payload(self) -> list[str]:
        return list(self.parts)


class Expression:
    def __and__(self, other: Expression) -> Expression:
        return AndExpression.build(self, _require_expression(other))

    def __or__(self, other: Expression) -> Expression:
        return OrExpression.build(self, _require_expression(other))

    def __invert__(self) -> Expression:
        return NotExpression(self)

    def __bool__(self) -> bool:
        raise TypeError(
            "Query expressions cannot be converted to bool; use '&', "
            "'|', and '~' to combine them"
        )


@dataclass(frozen=True, slots=True)
class ComparisonExpression(Expression):
    field: FieldPath
    operator: ComparisonOperator
    value: Any


@dataclass(frozen=True, slots=True)
class MembershipExpression(Expression):
    field: FieldPath
    operator: MembershipOperator
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class AndExpression(Expression):
    expressions: tuple[Expression, ...]

    @classmethod
    def build(cls, *values: Expression) -> Expression:
        expressions: list[Expression] = []

        for value in values:
            if isinstance(value, cls):
                expressions.extend(value.expressions)
            else:
                expressions.append(value)

        if len(expressions) == 1:
            return expressions[0]

        return cls(tuple(expressions))


@dataclass(frozen=True, slots=True)
class OrExpression(Expression):
    expressions: tuple[Expression, ...]

    @classmethod
    def build(cls, *values: Expression) -> Expression:
        expressions: list[Expression] = []

        for value in values:
            if isinstance(value, cls):
                expressions.extend(value.expressions)
            else:
                expressions.append(value)

        if len(expressions) == 1:
            return expressions[0]

        return cls(tuple(expressions))


@dataclass(frozen=True, slots=True)
class NotExpression(Expression):
    expression: Expression


@dataclass(frozen=True, slots=True, eq=False)
class Field:
    path: FieldPath

    def __init__(self, value: str | Iterable[str]) -> None:
        object.__setattr__(self, "path", FieldPath(value))

    def __eq__(self, value: object) -> ComparisonExpression: # pyright: ignore[reportIncompatibleMethodOverride]
        return self._compare(ComparisonOperator.EQ, value)

    def __ne__(self, value: object) -> ComparisonExpression: # pyright: ignore[reportIncompatibleMethodOverride]
        return self._compare(ComparisonOperator.NE, value)

    def __gt__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.GT, value)

    def __ge__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.GTE, value)

    def __lt__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.LT, value)

    def __le__(self, value: object) -> ComparisonExpression:
        return self._compare(ComparisonOperator.LTE, value)

    def in_(self, values: Iterable[Any]) -> MembershipExpression:
        return MembershipExpression(
            field=self.path,
            operator=MembershipOperator.IN,
            values=_membership_values(values),
        )

    def not_in(self, values: Iterable[Any]) -> MembershipExpression:
        return MembershipExpression(
            field=self.path,
            operator=MembershipOperator.NOT_IN,
            values=_membership_values(values),
        )

    def _compare(
        self,
        operator: ComparisonOperator,
        value: object,
    ) -> ComparisonExpression:
        return ComparisonExpression(
            field=self.path,
            operator=operator,
            value=value,
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


def _membership_values(values: Iterable[Any]) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(
            "Query membership values must be a non-string iterable"
        )

    return tuple(values)


def _require_expression(value: object) -> Expression:
    if not isinstance(value, Expression):
        raise TypeError(
            "Query logical operands must be query expressions"
        )

    return value
