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
        "source": _source_to_payload(query),
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

    if version not in {1, QUERY_PROTOCOL_VERSION}:
        raise ValueError(
            f"Unsupported query protocol version: {version!r}"
        )

    source = _source_from_payload(
        payload.get("source"),
        version=version,
    )
    joins_payload = payload.get("joins")

    if not isinstance(joins_payload, list):
        raise TypeError(
            "Serialized query joins must be a list"
        )

    joins = tuple(
        _join_from_payload(raw_join)
        for raw_join in joins_payload
    )
    raw_predicate = payload.get("predicate")
    predicate = (
        expression_from_payload(raw_predicate)
        if raw_predicate is not None
        else None
    )

    return Query(
        source=source,
        joins=joins,
        predicate=predicate,
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


def _source_to_payload(query: Query) -> dict[str, Any]:
    source = query.source

    if isinstance(source, Address):
        return {
            "address": source.to_list(),
        }

    schema_name = query.schema_name

    if schema_name is None:
        raise TypeError("Query source cannot be serialized")

    return {
        "schema": schema_name,
    }


def _source_from_payload(value: Any, *, version: int) -> Any:
    if version == 1:
        return Address(
            *_string_list(value, name="query source")
        )

    if not isinstance(value, dict):
        raise TypeError(
            "Serialized query source must be an object"
        )

    address = value.get("address")

    if address is not None:
        return Address(
            *_string_list(address, name="query source address")
        )

    schema = value.get("schema")

    if not isinstance(schema, str) or not schema:
        raise ValueError(
            "Serialized query source schema is invalid"
        )

    return schema


def _join_from_payload(payload: Any) -> JoinSpec:
    if not isinstance(payload, dict):
        raise TypeError(
            "Serialized query joins must be objects"
        )

    alias = payload.get("alias")

    if alias is not None:
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
