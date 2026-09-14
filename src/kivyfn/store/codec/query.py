# src/kivyfn/store/codec/query.py
from __future__ import annotations

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
)
from kivyfn.store.query.query import JoinKind, JoinSpec, Query
from kivyfn.store.query.result import QueryResult, QueryRow

if TYPE_CHECKING:
    from kivyfn.store.query.shape import (
        QueryFieldShape,
        QueryJoinShape,
        QueryShape,
    )


QUERY_PROTOCOL_VERSION = 1


def encode_query_rows(rows: QueryResult) -> list[dict[str, Any]]:
    """Encode complete result rows in their existing order."""
    return [{"id": row.id, "values": row.to_dict()} for row in rows]


def decode_query_rows(payload: object) -> QueryResult:
    """Decode ordered rows, rejecting duplicate result identities."""
    rows = tuple(
        _row_from_payload(value)
        for value in _require_list(payload, "query rows")
    )
    identities = tuple(row.id for row in rows)

    if len(identities) != len(set(identities)):
        raise ValueError("Serialized query rows contain duplicate IDs")

    return rows


def encode_query_shape(shape: QueryShape) -> dict[str, Any]:
    """Encode resolved query metadata without importing client schemas."""
    return {
        "root_schema": shape.root_schema,
        "live_compatible": shape.live_compatible,
        "joins": [
            {
                "owner_path": list(join.owner_path),
                "relation_path": list(join.relation_path),
                "field_name": join.field_name,
                "output_name": join.output_name,
                "output_prefix": join.output_prefix,
                "target_schema": join.target_schema,
                "kind": join.kind.value,
            }
            for join in shape.joins
        ],
        "fields": [
            {
                "name": field.name,
                "schema": field.schema,
                "field": field.field,
                "relation_path": list(field.relation_path),
                "nullable": field.nullable,
            }
            for field in shape.fields
        ],
    }


def decode_query_shape(payload: object) -> QueryShape:
    """Reconstruct typed query metadata without resolving Store nodes."""
    from kivyfn.store.query.shape import QueryShape

    data = _require_dict(payload, "query shape")
    joins = tuple(
        _join_shape_from_payload(value)
        for value in _require_list(data.get("joins"), "query shape joins")
    )
    fields = tuple(
        _field_shape_from_payload(value)
        for value in _require_list(data.get("fields"), "query shape fields")
    )
    paths = tuple(join.relation_path for join in joins)
    names = tuple(field.name for field in fields)

    if len(paths) != len(set(paths)):
        raise ValueError("Serialized query shape contains duplicate joins")

    if len(names) != len(set(names)):
        raise ValueError("Serialized query shape contains duplicate fields")

    return QueryShape(
        root_schema=_query_string(data, "root_schema"),
        joins=joins,
        fields=fields,
        live_compatible=_shape_bool(data, "live_compatible"),
    )


def query_to_payload(query: Query[Any]) -> dict[str, Any]:
    return {
        "version": QUERY_PROTOCOL_VERSION,
        "source": _source_to_payload(query),
        "joins": [_join_to_payload(join) for join in query.joins],
        "predicate": (
            expression_to_payload(query.predicate)
            if query.predicate is not None
            else None
        ),
    }


def query_from_payload(payload: Any) -> Query[Any]:
    data = _require_dict(payload, "query")

    version = data.get("version")

    if type(version) is not int or version != QUERY_PROTOCOL_VERSION:
        raise ValueError(f"Unsupported query protocol version: {version!r}")

    joins = _require_list(data.get("joins"), "query joins")

    predicate_payload = data.get("predicate")

    return Query(
        source=_source_from_payload(data.get("source")),
        joins=tuple(_join_from_payload(join) for join in joins),
        predicate=(
            expression_from_payload(predicate_payload)
            if predicate_payload is not None
            else None
        ),
    )


def expression_to_payload(expression: Expression) -> dict[str, Any]:
    match expression:
        case ComparisonExpression():
            return {
                "type": "comparison",
                "field": list(expression.field.parts),
                "operator": expression.operator.value,
                "value": expression.value,
            }

        case MembershipExpression():
            return {
                "type": "membership",
                "field": list(expression.field.parts),
                "operator": expression.operator.value,
                "values": list(expression.values),
            }

        case AndExpression():
            return {
                "type": "and",
                "expressions": [
                    expression_to_payload(child)
                    for child in expression.expressions
                ],
            }

        case OrExpression():
            return {
                "type": "or",
                "expressions": [
                    expression_to_payload(child)
                    for child in expression.expressions
                ],
            }

        case NotExpression():
            return {
                "type": "not",
                "expression": expression_to_payload(expression.expression),
            }

    raise TypeError(
        f"Unsupported query expression: {type(expression).__name__}"
    )


def expression_from_payload(payload: Any) -> Expression:
    data = _require_dict(payload, "query expression")

    expression_type = data.get("type")

    if expression_type == "comparison":
        if "value" not in data:
            raise ValueError("Serialized comparison must contain a value")

        return ComparisonExpression(
            field=FieldPath(
                _string_parts(data.get("field"), "comparison field")
            ),
            operator=ComparisonOperator(data.get("operator")),
            value=data["value"],
        )

    if expression_type == "membership":
        values = _require_list(data.get("values"), "membership values")

        return MembershipExpression(
            field=FieldPath(
                _string_parts(data.get("field"), "membership field")
            ),
            operator=MembershipOperator(data.get("operator")),
            values=tuple(values),
        )

    if expression_type == "and":
        return AndExpression(
            tuple(
                expression_from_payload(child)
                for child in _logical_values(data)
            )
        )

    if expression_type == "or":
        return OrExpression(
            tuple(
                expression_from_payload(child)
                for child in _logical_values(data)
            )
        )

    if expression_type == "not":
        return NotExpression(expression_from_payload(data.get("expression")))

    raise ValueError(f"Unknown query expression type: {expression_type!r}")


def _source_to_payload(query: Query[Any]) -> dict[str, Any]:
    source = query.source

    if isinstance(source, Address):
        return {"address": source.to_list()}

    schema_name = query.schema_name

    if schema_name is None:
        raise TypeError("Query source cannot be serialized")

    return {"schema": schema_name}


def _source_from_payload(payload: Any) -> Address | str:
    data = _require_dict(payload, "query source")

    has_address = "address" in data
    has_schema = "schema" in data

    if has_address == has_schema:
        raise ValueError(
            "Serialized query source must contain exactly "
            "one of 'address' or 'schema'"
        )

    if has_address:
        return Address(*_string_parts(data["address"], "query source address"))

    schema = data["schema"]

    if not isinstance(schema, str) or not schema:
        raise ValueError(
            "Serialized query source schema must be a non-empty string"
        )

    return schema


def _join_to_payload(join: JoinSpec) -> dict[str, Any]:
    return {
        "path": list(join.path.parts),
        "kind": join.kind.value,
        "alias": join.alias,
    }


def _join_from_payload(payload: Any) -> JoinSpec:
    data = _require_dict(payload, "query join")
    alias = _query_string(data, "alias")

    if "." in alias:
        raise ValueError("Serialized query join alias cannot contain dots")

    return JoinSpec(
        path=FieldPath(_string_parts(data.get("path"), "query join path")),
        kind=JoinKind(data.get("kind")),
        alias=alias,
    )


def _logical_values(payload: dict[str, Any]) -> list[Any]:
    values = _require_list(payload.get("expressions"), "logical expressions")

    if not values:
        raise ValueError("Serialized logical expressions cannot be empty")

    return values


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"Serialized {name} must be an object")

    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"Serialized {name} must be a list")

    return value


def _string_parts(
    value: Any, name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    values = _require_list(value, name)

    if not values and not allow_empty:
        raise ValueError(f"Serialized {name} cannot be empty")

    if not all(isinstance(part, str) and part for part in values):
        raise ValueError(f"Serialized {name} must contain non-empty strings")

    return tuple(values)


def _row_from_payload(payload: object) -> QueryRow:
    data = _require_dict(payload, "query row")
    values = _require_dict(data.get("values"), "query row values")

    if any(not isinstance(key, str) or not key for key in values):
        raise ValueError("Serialized query row field names must be strings")

    return QueryRow(_query_string(data, "id"), values)


def _join_shape_from_payload(payload: object) -> QueryJoinShape:
    from kivyfn.store.query.shape import QueryJoinShape

    data = _require_dict(payload, "query join shape")

    return QueryJoinShape(
        owner_path=_string_parts(
            data.get("owner_path"), "join owner path", allow_empty=True
        ),
        relation_path=_string_parts(data.get("relation_path"), "join path"),
        field_name=_query_string(data, "field_name"),
        output_name=_query_string(data, "output_name"),
        output_prefix=_query_string(data, "output_prefix"),
        target_schema=_query_string(data, "target_schema"),
        kind=JoinKind(data.get("kind")),
    )


def _field_shape_from_payload(payload: object) -> QueryFieldShape:
    from kivyfn.store.query.shape import QueryFieldShape

    data = _require_dict(payload, "query field shape")

    return QueryFieldShape(
        name=_query_string(data, "name"),
        schema=_query_string(data, "schema"),
        field=_query_string(data, "field"),
        relation_path=_string_parts(
            data.get("relation_path"), "field relation path", allow_empty=True
        ),
        nullable=_shape_bool(data, "nullable"),
    )


def _query_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)

    if not isinstance(value, str) or not value:
        raise ValueError(f"Serialized query {key} must be a non-empty string")

    return value


def _shape_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)

    if not isinstance(value, bool):
        raise ValueError(f"Query shape {key} must be a boolean")

    return value
