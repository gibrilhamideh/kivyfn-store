from __future__ import annotations

from typing import Any

import pytest

from kivyfn.store.query import Query, field
from kivyfn.store.query.expression import (
    AndExpression,
    ComparisonExpression,
    ComparisonOperator,
    Expression,
    FieldPath,
    OrExpression,
    iter_field_paths,
)
from kivyfn.store.query.plan import evaluate_expression
from tests.query.conftest import QueryArticleSchema


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (field("score") == 20, True),
        (field("score") != 20, False),
        (field("score") > 19, True),
        (field("score") >= 20, True),
        (field("score") < 20, False),
        (field("score") <= 20, True),
        (field("score").in_([10, 20]), True),
        (field("score").not_in([10, 20]), False),
        (field("score").not_in([10, 30]), True),
        ((field("score") == 20) & (field("name") == "Ada"), True),
        ((field("score") == 0) | (field("name") == "Ada"), True),
        (~(field("score") == 0), True),
        (field("author.score") == 7, True),
        (field("missing") != 3, False),
        (field("score.invalid") == 3, False),
    ],
)
def test_predicate_operators(expression: Expression, expected: bool) -> None:
    row = {"score": 20, "name": "Ada", "author": {"score": 7}}
    assert evaluate_expression(expression, row) is expected


@pytest.mark.parametrize("operator", list(ComparisonOperator))
@pytest.mark.parametrize("null_side", ["left", "right", "both"])
def test_null_comparisons_have_defined_semantics(
    operator: ComparisonOperator, null_side: str
) -> None:
    left = None if null_side in {"left", "both"} else 20
    right = None if null_side in {"right", "both"} else 20
    expression = ComparisonExpression(FieldPath("score"), operator, right)
    expected = False
    if operator is ComparisonOperator.EQ:
        expected = left == right
    elif operator is ComparisonOperator.NE:
        expected = left != right
    assert evaluate_expression(expression, {"score": left}) is expected


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (field("author.score") == None, True),  # noqa: E711
        (field("author.score").in_([None, 20]), True),
        (field("author.score").not_in([None, 20]), False),
        (field("author.score") > 20, False),
        (~(field("author.score") > 20), True),
    ],
)
def test_null_join_paths(expression: Expression, expected: bool) -> None:
    assert evaluate_expression(expression, {"author": None}) is expected


def test_logical_builders_flatten_without_mutating_descriptions() -> None:
    first = field("a") == 1
    second = field("b") == 2
    third = field("c") == 3
    joined = (first & second) & third
    assert isinstance(joined, AndExpression)
    assert joined.expressions == (first, second, third)
    assert ((first | second) | third) == OrExpression((first, second, third))
    assert AndExpression.build(first) is first
    assert list(iter_field_paths(~joined)) == [
        FieldPath("a"),
        FieldPath("b"),
        FieldPath("c"),
    ]


def test_query_hash_and_equality_match_immutable_descriptions() -> None:
    query = Query(QueryArticleSchema)
    filtered = query.where(field("published") == True)  # noqa: E712
    same = Query(QueryArticleSchema).where(
        field("published") == True  # noqa: E712
    )
    assert query.predicate is None
    assert filtered == same
    assert len({filtered, same}) == 1
    assert filtered != filtered.where(field("title") == "First")


@pytest.mark.parametrize("value", [[], {}, ([1],), ({"a": 1},)])
def test_comparison_rejects_deeply_unhashable_values(value: Any) -> None:
    with pytest.raises(TypeError, match="hashable"):
        _ = field("payload") == value
    with pytest.raises(TypeError, match="hashable"):
        field("payload").in_([value])


@pytest.mark.parametrize("value", ["", ".a", "a.", "a..b", ()])
def test_field_path_rejects_empty_components(value: Any) -> None:
    with pytest.raises(ValueError, match="empty"):
        FieldPath(value)


def test_field_path_copies_iterables_and_rejects_non_strings() -> None:
    parts = ["author", "name"]
    path = FieldPath(parts)
    parts.append("mutated")
    assert str(path) == "author.name"
    assert path == FieldPath("author.name")
    with pytest.raises(TypeError, match="strings"):
        FieldPath(["author", 1])  # type: ignore[list-item]


@pytest.mark.parametrize("value", ["abc", b"abc", bytearray(b"abc"), []])
def test_membership_requires_nonempty_nonstring_iterable(value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        field("name").in_(value)


def test_expressions_reject_python_boolean_operators() -> None:
    predicate = field("a") == 1
    with pytest.raises(TypeError, match="cannot be converted"):
        bool(predicate)
    with pytest.raises(TypeError, match="operands"):
        predicate & True  # type: ignore[operator]
    with pytest.raises(ValueError, match="cannot be empty"):
        AndExpression.build()
    with pytest.raises(ValueError, match="requires at least"):
        Query(QueryArticleSchema).where()


def test_logical_evaluation_short_circuits() -> None:
    unsafe = field("mixed") > "text"
    assert evaluate_expression(
        (field("enabled") == True) | unsafe,  # noqa: E712
        {"enabled": True, "mixed": 3},
    )
    assert not evaluate_expression(
        (field("enabled") == False) & unsafe,  # noqa: E712
        {"enabled": True, "mixed": 3},
    )
