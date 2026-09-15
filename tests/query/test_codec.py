from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from kivyfn.store import Query, Store, field
from kivyfn.store.address import Address
from kivyfn.store.codec.query import (
    decode_query_rows,
    decode_query_shape,
    encode_query_rows,
    encode_query_shape,
    expression_from_payload,
    expression_to_payload,
    query_from_payload,
    query_to_payload,
)
from kivyfn.store.query.expression import Expression
from kivyfn.store.query.result import QueryRow
from tests.query.conftest import QueryArticleSchema


@pytest.mark.parametrize(
    "query",
    [
        Query(QueryArticleSchema),
        Query("QueryArticleSchema"),
        Query(Address("articles")),
        Query(Address()),
        Query(QueryArticleSchema)
        .join("author_id", as_="author")
        .left_join("author.country_id", as_="country")
        .where(
            (field("author.nickname") == "Ada")
            | ~(field("author.score").in_([20, None]))
        ),
        Query(QueryArticleSchema).where(field("payload") == (1, (2, "three"))),
        Query(QueryArticleSchema).where(
            field("payload").not_in([(1, 2), (3, 4)])
        ),
    ],
)
def test_query_wire_json_roundtrip_preserves_description(
    query: Query[Any],
) -> None:
    payload = json.loads(json.dumps(query_to_payload(query)))
    restored = query_from_payload(payload)
    assert restored == query
    assert hash(restored) == hash(query)


@pytest.mark.parametrize(
    "expression",
    [
        field("a") == 1,
        field("a") != None,  # noqa: E711
        field("a") > 1,
        field("a") >= 1,
        field("a") < 1,
        field("a") <= 1,
        field("a").in_([1, "x", None]),
        field("a").not_in([False]),
        (field("a") == 1) & (field("b") == 2),
        (field("a") == 1) | (field("b") == 2),
        ~(field("a") == 1),
    ],
)
def test_expression_wire_roundtrip(expression: Expression) -> None:
    restored = expression_from_payload(expression_to_payload(expression))
    assert restored == expression


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("version", True),
        ("version", 1.0),
        ("version", 2),
        ("version", None),
        ("source", {}),
        ("source", {"schema": "X", "address": []}),
        ("source", {"schema": ""}),
        ("source", {"schema": 5}),
        ("source", {"address": "articles"}),
        ("source", {"address": [""]}),
        ("source", {"address": [3]}),
        ("joins", {}),
        ("joins", [None]),
        ("joins", [{"path": [], "kind": "left", "alias": "author"}]),
        ("joins", [{"path": ["author_id"], "kind": "invalid", "alias": "a"}]),
        ("joins", [{"path": ["author_id"], "kind": "left", "alias": ""}]),
        ("joins", [{"path": ["author_id"], "kind": "left", "alias": "a.b"}]),
    ],
)
def test_malformed_query_wire_payload_rejected(key: str, value: Any) -> None:
    payload = query_to_payload(Query(QueryArticleSchema))
    payload[key] = value
    with pytest.raises((ValueError, TypeError)):
        query_from_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"type": "unsupported"},
        {"type": "comparison", "field": ["a"], "operator": "eq"},
        {"type": "comparison", "field": ["a"], "operator": "??", "value": 1},
        {"type": "comparison", "field": [], "operator": "eq", "value": 1},
        {"type": "comparison", "field": [1], "operator": "eq", "value": 1},
        {"type": "comparison", "field": ["a"], "operator": "eq", "value": {}},
        {"type": "membership", "field": ["a"], "operator": "in", "values": []},
        {
            "type": "membership",
            "field": ["a"],
            "operator": "in",
            "values": "x",
        },
        {
            "type": "membership",
            "field": ["a"],
            "operator": "bad",
            "values": [1],
        },
        {
            "type": "membership",
            "field": ["a"],
            "operator": "in",
            "values": [{}],
        },
        {"type": "and", "expressions": []},
        {"type": "or", "expressions": "x"},
        {"type": "not"},
    ],
)
def test_malformed_expression_wire_payload_rejected(payload: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        expression_from_payload(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), frozenset({1})])
def test_nonportable_query_literals_rejected_at_transport_boundary(
    value: Any,
) -> None:
    expression = field("payload") == value
    with pytest.raises(ValueError, match="finite JSON scalars"):
        expression_to_payload(expression)


def test_query_row_wire_roundtrip_and_ownership() -> None:
    rows = (QueryRow("r", {"data": {"items": [1]}}),)
    payload = encode_query_rows(rows)
    decoded = decode_query_rows(payload)
    payload[0]["values"]["data"]["items"].append(2)
    assert decoded == rows
    assert decoded[0]["data"] == {"items": [1]}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [None],
        [{"id": "", "values": {}}],
        [{"id": "r", "values": []}],
        [{"id": "r", "values": {"": 1}}],
        [{"id": "r", "values": {1: 1}}],
        [{"id": "r", "values": {}}, {"id": "r", "values": {}}],
    ],
)
def test_malformed_query_row_payload_rejected(payload: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_query_rows(payload)


def test_nested_query_shape_wire_roundtrip(query_store: Store) -> None:
    shape = query_store.compile_query(
        Query(QueryArticleSchema)
        .left_join("author_id", as_="author")
        .left_join("author.country_id", as_="country")
    ).shape
    encoded = encode_query_shape(shape)
    assert decode_query_shape(json.loads(json.dumps(encoded))) == shape


@pytest.mark.parametrize(
    ("group", "key", "value"),
    [
        ("root", "root_schema", ""),
        ("root", "live_compatible", 1),
        ("root", "joins", {}),
        ("root", "fields", {}),
        ("join", "owner_path", [""]),
        ("join", "relation_path", []),
        ("join", "field_name", ""),
        ("join", "output_name", 3),
        ("join", "output_prefix", ""),
        ("join", "target_schema", None),
        ("join", "kind", "unsupported"),
        ("field", "name", ""),
        ("field", "schema", 3),
        ("field", "field", ""),
        ("field", "relation_path", [False]),
        ("field", "nullable", 0),
    ],
)
def test_malformed_query_shape_fields_rejected(
    query_store: Store, group: str, key: str, value: Any
) -> None:
    shape = query_store.compile_query(
        Query(QueryArticleSchema).left_join("author_id", as_="author")
    ).shape
    payload = encode_query_shape(shape)
    target = payload
    if group == "join":
        target = payload["joins"][0]
    elif group == "field":
        target = payload["fields"][0]
    target[key] = value
    with pytest.raises((TypeError, ValueError)):
        decode_query_shape(payload)


@pytest.mark.parametrize("group", ["joins", "fields"])
def test_duplicate_query_shape_paths_and_names_rejected(
    query_store: Store, group: str
) -> None:
    shape = query_store.compile_query(
        Query(QueryArticleSchema).left_join("author_id", as_="author")
    ).shape
    payload = encode_query_shape(shape)
    payload[group].append(deepcopy(payload[group][0]))
    with pytest.raises(ValueError, match="duplicate"):
        decode_query_shape(payload)
