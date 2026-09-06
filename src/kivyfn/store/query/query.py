from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeAlias,
    TypeVar,
)

from kivyfn.store.address import Address
from kivyfn.store.query.expression import (
    AndExpression,
    Expression,
    FieldPath,
)


if TYPE_CHECKING:
    from kivyfn.store.schema import ManySchema


QUERY_PROTOCOL_VERSION = 2
SINGLE_QUERY_ROW_ID = "__single__"

QuerySource: TypeAlias = Address[Any] | type["ManySchema"] | str

SchemaT = TypeVar(
    "SchemaT",
    bound="ManySchema",
)


class JoinKind(str, Enum):
    INNER = "inner"
    LEFT = "left"


@dataclass(frozen=True, slots=True)
class JoinSpec:
    path: FieldPath
    kind: JoinKind
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class Query(Generic[SchemaT]):
    source: Address[Any] | type[SchemaT] | str
    joins: tuple[JoinSpec, ...] = ()
    predicate: Expression | None = None
    protocol_version: int = QUERY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not _valid_source(self.source):
            raise TypeError(
                (
                    "Query source must be an Address, "
                    "ManySchema class, or schema name"
                )
            )

        if self.protocol_version != QUERY_PROTOCOL_VERSION:
            raise ValueError(
                (
                    "Unsupported in-memory query protocol version: "
                    f"{self.protocol_version}"
                )
            )

    def where(
        self,
        *expressions: Expression,
    ) -> Query[SchemaT]:
        if not expressions:
            raise ValueError(
                "Query.where() requires at least one expression"
            )

        checked = tuple(
            _require_expression(expression)
            for expression in expressions
        )

        predicate = AndExpression.build(
            *checked,
        )

        if self.predicate is not None:
            predicate = AndExpression.build(
                self.predicate,
                predicate,
            )

        return replace(
            self,
            predicate=predicate,
        )

    def join(
        self,
        path: str,
        *,
        as_: str | None = None,
    ) -> Query[SchemaT]:
        return self._join(
            path,
            alias=as_,
            kind=JoinKind.LEFT,
        )

    def inner_join(
        self,
        path: str,
        *,
        as_: str | None = None,
    ) -> Query[SchemaT]:
        return self._join(
            path,
            alias=as_,
            kind=JoinKind.INNER,
        )

    def outerjoin(
        self,
        path: str,
        *,
        as_: str | None = None,
    ) -> Query[SchemaT]:
        return self.join(
            path,
            as_=as_,
        )

    def resolved(
        self,
        address: Address[Any],
    ) -> Query[SchemaT]:
        if not isinstance(address, Address):
            raise TypeError(
                "Query address must be an Address"
            )

        return replace(
            self,
            source=address,
        )

    @property
    def schema_name(self) -> str | None:
        source = self.source

        if isinstance(source, str):
            return source

        if isinstance(source, type):
            return source.__name__

        return None

    def _join(
        self,
        path: str,
        *,
        alias: str | None,
        kind: JoinKind,
    ) -> Query[SchemaT]:
        field_path = FieldPath(path)

        if alias is not None:
            if not isinstance(alias, str) or not alias:
                raise ValueError(
                    (
                        "Query join aliases must be "
                        "non-empty strings"
                    )
                )

            if "." in alias:
                raise ValueError(
                    "Query join aliases cannot contain dots"
                )

        if any(
            join.path == field_path
            for join in self.joins
        ):
            raise ValueError(
                f"Query join path {path!r} is already used"
            )

        return replace(
            self,
            joins=(
                *self.joins,
                JoinSpec(
                    path=field_path,
                    kind=kind,
                    alias=alias,
                ),
            ),
        )


def _valid_source(source: object) -> bool:
    if isinstance(source, Address):
        return True

    if isinstance(source, str):
        return bool(source)

    if not isinstance(source, type):
        return False

    from kivyfn.store.schema import ManySchema

    return issubclass(
        source,
        ManySchema,
    )


def _require_expression(
    value: object,
) -> Expression:
    if not isinstance(value, Expression):
        raise TypeError(
            (
                "Query.where() arguments must be "
                "query expressions"
            )
        )

    return value
