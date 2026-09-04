from __future__ import annotations

from typing import Any

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
)
from kivyfn.store.query.query import (
    QUERY_PROTOCOL_VERSION,
    JoinKind,
    JoinSpec,
    Query,
)


def query_to_payload(query: Query) -> dict[str, Any]:
    return {
        "version": query.protocol_version,
        "source": query.source.to_list(),
        "joins": [
            {
                "path": join.path.payload(),
                "alias": join.alias,
                "kind": join.kind.value,
            }
            for join in query.joins
        ],
        "predicate": (
            expression_to_payload(query.predicate)
            if query.predicate is not None
            else None
        ),
    }


def query_from_payload(payload: Any) -> Query:
    if not isinstance(payload, dict):
        raise TypeError(
            "Serialized query must be an object"
        )

    version = payload.get("version")

    if version != QUERY_PROTOCOL_VERSION:
        raise ValueError(
            f"Unsupported query protocol version: {version!r}"
        )

    source = _string_list(
        payload.get("source"),
        name="query source",
    )
    joins_payload = payload.get("joins")

    if not isinstance(joins_payload, list):
        raise TypeError(
            "Serialized query joins must be a list"
        )

    joins: list[JoinSpec] = []

    for raw_join in joins_payload:
        joins.append(_join_from_payload(raw_join))

    raw_predicate = payload.get("predicate")
    predicate = (
        expression_from_payload(raw_predicate)
        if raw_predicate is not None
        else None
    )

    return Query(
        source=Address(*source),
        joins=tuple(joins),
        predicate=predicate,
        protocol_version=version,
    )


def expression_to_payload(expression: Expression) -> dict[str, Any]:
    if isinstance(expression, ComparisonExpression):
        return {
            "type": "comparison",
            "field": expression.field.payload(),
            "operator": expression.operator.value,
            "value": expression.value,
        }

    if isinstance(expression, MembershipExpression):
        return {
            "type": "membership",
            "field": expression.field.payload(),
            "operator": expression.operator.value,
            "values": list(expression.values),
        }

    if isinstance(expression, AndExpression):
        return {
            "type": "and",
            "expressions": [
                expression_to_payload(child)
                for child in expression.expressions
            ],
        }

    if isinstance(expression, OrExpression):
        return {
            "type": "or",
            "expressions": [
                expression_to_payload(child)
                for child in expression.expressions
            ],
        }

    if isinstance(expression, NotExpression):
        return {
            "type": "not",
            "expression": expression_to_payload(
                expression.expression,
            ),
        }

    raise TypeError(
        f"Unsupported query expression: {type(expression).__name__}"
    )


def expression_from_payload(payload: Any) -> Expression:
    if not isinstance(payload, dict):
        raise TypeError(
            "Serialized query expression must be an object"
        )

    expression_type = payload.get("type")

    if expression_type == "comparison":
        return ComparisonExpression(
            field=FieldPath(
                _string_list(
                    payload.get("field"),
                    name="comparison field",
                )
            ),
            operator=ComparisonOperator(
                payload.get("operator"),
            ),
            value=payload.get("value"),
        )

    if expression_type == "membership":
        values = payload.get("values")

        if not isinstance(values, list):
            raise TypeError(
                "Serialized membership values must be a list"
            )

        return MembershipExpression(
            field=FieldPath(
                _string_list(
                    payload.get("field"),
                    name="membership field",
                )
            ),
            operator=MembershipOperator(
                payload.get("operator"),
            ),
            values=tuple(values),
        )

    if expression_type in {"and", "or"}:
        values = payload.get("expressions")

        if not isinstance(values, list) or not values:
            raise ValueError(
                "Serialized logical expressions must be a non-empty list"
            )

        children = tuple(
            expression_from_payload(value)
            for value in values
        )

        if expression_type == "and":
            return AndExpression(children)

        return OrExpression(children)

    if expression_type == "not":
        return NotExpression(
            expression_from_payload(
                payload.get("expression"),
            )
        )

    raise ValueError(
        f"Unknown query expression type: {expression_type!r}"
    )


def _join_from_payload(payload: Any) -> JoinSpec:
    if not isinstance(payload, dict):
        raise TypeError(
            "Serialized query joins must be objects"
        )

    alias = payload.get("alias")

    if not isinstance(alias, str) or not alias:
        raise ValueError(
            "Serialized query join alias is invalid"
        )

    return JoinSpec(
        path=FieldPath(
            _string_list(
                payload.get("path"),
                name="join path",
            )
        ),
        alias=alias,
        kind=JoinKind(payload.get("kind")),
    )


def _string_list(value: Any, *, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"Serialized {name} must be a non-empty list"
        )

    if not all(isinstance(part, str) and part for part in value):
        raise ValueError(
            f"Serialized {name} must contain non-empty strings"
        )

    return value
