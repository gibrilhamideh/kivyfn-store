from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from kivyfn.store.address import Address
from kivyfn.store.query.expression import (
    AndExpression,
    Expression,
    FieldPath,
)


QUERY_PROTOCOL_VERSION = 1


class JoinKind(str, Enum):
    INNER = "inner"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class JoinSpec:
    path: FieldPath
    alias: str
    kind: JoinKind


@dataclass(frozen=True, slots=True)
class Query:
    source: Address[Any]
    joins: tuple[JoinSpec, ...] = ()
    predicate: Expression | None = None
    protocol_version: int = QUERY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.source, Address):
            raise TypeError(
                "Query source must be an Address"
            )

        if self.protocol_version != QUERY_PROTOCOL_VERSION:
            raise ValueError(
                "Unsupported in-memory query protocol version: "
                f"{self.protocol_version}"
            )

    def where(self, *expressions: Expression) -> Query:
        if not expressions:
            raise ValueError(
                "Query.where() requires at least one expression"
            )

        checked = tuple(
            _require_expression(expression)
            for expression in expressions
        )
        predicate = AndExpression.build(*checked)

        if self.predicate is not None:
            predicate = AndExpression.build(
                self.predicate,
                predicate,
            )

        return replace(
            self,
            predicate=predicate,
        )

    def join(self, path: str, *, as_: str) -> Query:
        return self._join(
            path,
            alias=as_,
            kind=JoinKind.INNER,
        )

    def outerjoin(self, path: str, *, as_: str) -> Query:
        return self._join(
            path,
            alias=as_,
            kind=JoinKind.LEFT,
        )

    def _join(
        self,
        path: str,
        *,
        alias: str,
        kind: JoinKind,
    ) -> Query:
        if not isinstance(alias, str) or not alias:
            raise ValueError(
                "Query join aliases must be non-empty strings"
            )

        if "." in alias:
            raise ValueError(
                "Query join aliases cannot contain dots"
            )

        if any(join.alias == alias for join in self.joins):
            raise ValueError(
                f"Query join alias {alias!r} is already used"
            )

        return replace(
            self,
            joins=(
                *self.joins,
                JoinSpec(
                    path=FieldPath(path),
                    alias=alias,
                    kind=kind,
                ),
            ),
        )


def _require_expression(value: object) -> Expression:
    if not isinstance(value, Expression):
        raise TypeError(
            "Query.where() arguments must be query expressions"
        )

    return value
