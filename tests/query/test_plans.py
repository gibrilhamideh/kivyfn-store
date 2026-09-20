from __future__ import annotations

from typing import Any

import pytest

from kivyfn.store.store import Store
from kivyfn.store.query import Query, field
from kivyfn.store.address import Address
from kivyfn.store.error import StoreOperationError
from kivyfn.store.protocol.message.requests import RemoveRequestAction
from kivyfn.store.query.result import QueryRow
from kivyfn.store.query.shape import QueryShape, query_field_metadata
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.fields import ForeignKeyField, StringField
from tests.query.conftest import (
    QueryArticleSchema,
    QueryAuthorSchema,
    QueryRootSchema,
    execute,
    update,
)


def ids(store: Store, query: Query[Any]) -> list[str]:
    return [row.id for row in store.query(query)]


@pytest.mark.parametrize(
    "source", [QueryArticleSchema, "QueryArticleSchema", Address("articles")]
)
def test_source_forms_preserve_canonical_root_identity(
    query_store: Store, source: Any
) -> None:
    result = query_store.query(Query(source))
    assert [row.id for row in result] == [
        "article-one",
        "article-two",
        "article-orphan",
    ]
    assert result[0]["title"] == "First"
    assert len(result[0]) == 5
    assert set(result[0]) == {
        "title",
        "published",
        "author_id",
        "reviewer_id",
        "payload",
    }


def test_inner_and_left_join_null_membership(query_store: Store) -> None:
    inner = Query(QueryArticleSchema).join("author_id", as_="author")
    left = Query(QueryArticleSchema).left_join("author_id", as_="author")
    assert ids(query_store, inner) == ["article-one", "article-two"]
    result = query_store.query(left)
    assert result[-1]["author_nickname"] is None
    assert result[-1]["author_score"] is None
    assert ids(query_store, left.where(field("author.score") > 18)) == [
        "article-one"
    ]
    assert ids(
        query_store,
        left.where(field("author.score") == None),  # noqa: E711
    ) == ["article-two", "article-orphan"]


def test_nested_joins_and_independent_foreign_keys(query_store: Store) -> None:
    query = (
        Query(QueryArticleSchema)
        .join("author_id", as_="author")
        .left_join("author.country_id", as_="country")
        .left_join("reviewer_id", as_="reviewer")
        .left_join("reviewer.country_id", as_="review_country")
    )
    result = query_store.query(query)
    first = result[0]
    assert first.id == "article-one"
    assert first["author_nickname"] == "Ada"
    assert first["author_country_label"] == "North"
    assert first["reviewer_nickname"] == "Cy"
    assert first["reviewer_review_country_label"] == "South"
    assert result[1]["author_country_label"] is None
    assert result[1]["reviewer_review_country_label"] is None
    assert ids(
        query_store, query.where(field("author.country.label") == "North")
    ) == ["article-one"]


def test_inner_descendant_requires_present_left_parent(
    query_store: Store,
) -> None:
    query = (
        Query(QueryArticleSchema)
        .left_join("author_id", as_="author")
        .join("author.country_id", as_="country")
    )
    assert ids(query_store, query) == ["article-one"]


def test_nullable_dependency_change_does_not_crash(query_store: Store) -> None:
    query = (
        Query(QueryArticleSchema)
        .left_join("author_id", as_="author")
        .where(field("author.score") > 18)
    )
    assert ids(query_store, query) == ["article-one"]
    execute(query_store, update(QueryAuthorSchema, "author-a", score=None))
    assert ids(query_store, query) == []
    execute(query_store, update(QueryAuthorSchema, "author-a", score=45))
    assert ids(query_store, query) == ["article-one"]


def test_retarget_and_remove_target_have_final_query_projection(
    query_store: Store,
) -> None:
    query = Query(QueryArticleSchema).left_join("author_id", as_="author")
    execute(
        query_store,
        update(QueryArticleSchema, "article-one", author_id="author-c"),
    )
    assert query_store.query(query)[0]["author_nickname"] == "Cy"
    execute(
        query_store,
        update(
            QueryArticleSchema, "article-one", author_id=None, reviewer_id=None
        ),
        RemoveRequestAction("QueryAuthorSchema", "author-c"),
    )
    assert query_store.query(query)[0]["author_nickname"] is None


@pytest.mark.parametrize(
    "query",
    [
        Query(QueryArticleSchema).join("author_id", as_="author_id"),
        Query(QueryArticleSchema).join("author_id", as_="title"),
        Query(QueryArticleSchema)
        .join("author_id", as_="author")
        .join("author.country_id", as_="nickname"),
    ],
)
def test_alias_cannot_shadow_owner_field(
    query_store: Store, query: Query[Any]
) -> None:
    with pytest.raises(ValueError, match="shadows a field"):
        query_store.compile_query(query)


@pytest.mark.parametrize(
    "query",
    [
        Query(QueryArticleSchema).join("author.country_id", as_="country"),
        Query(QueryArticleSchema).join("title", as_="title_join"),
        Query(QueryArticleSchema).where(field("missing_join.name") == "x"),
        Query(QueryArticleSchema).where(field("missing") == "x"),
        Query(QueryArticleSchema)
        .join("author_id", as_="author")
        .where(field("author.missing") == "x"),
        Query("QueryRootSchema"),
        Query(Address()),
    ],
)
def test_invalid_plan_rejected_before_execution(
    query_store: Store, query: Query[Any]
) -> None:
    with pytest.raises((ValueError, StoreOperationError)):
        query_store.compile_query(query)
    assert query_store.booted


def test_flattened_field_collisions_are_rejected() -> None:
    class CollisionArticleSchema(ManySchema):
        author_id = ForeignKeyField("QueryAuthorSchema")
        author_nickname = StringField(default="")

    query = Query(CollisionArticleSchema).join("author_id", as_="author")
    with pytest.raises(ValueError, match="output field already exists"):
        QueryShape.build(
            query, CollisionArticleSchema(), lambda _: QueryAuthorSchema()
        )


def test_shape_metadata_tracks_nested_canonical_sources(
    query_store: Store,
) -> None:
    query = (
        Query(QueryArticleSchema)
        .join("author_id", as_="author")
        .left_join("author.country_id", as_="country")
    )
    shape = query_store.compile_query(query).shape
    root = shape.field("title")
    assert (root.schema, root.field, root.relation_path) == (
        "QueryArticleSchema",
        "title",
        (),
    )
    country = shape.field("author_country_label")
    assert (country.schema, country.field, country.relation_path) == (
        "QueryCountrySchema",
        "label",
        ("author", "country"),
    )
    assert country.joined and country.nullable
    assert not shape.field("author_nickname").nullable
    assert (
        shape
        == query_store.compile_query(
            query.where(field("published") == True)  # noqa: E712
        ).shape
    )
    assert len({shape, shape}) == 1
    metadata = query_field_metadata(
        shape, lambda name: query_store.get_node_by_schema(name).schema
    )
    assert metadata["author_nickname"] is QueryAuthorSchema().get_field(
        "nickname"
    )
    with pytest.raises(KeyError):
        shape.field("missing")


def test_query_rows_do_not_expose_mutable_snapshot_ownership(
    query_store: Store,
) -> None:
    row = query_store.query(Query(QueryArticleSchema))[0]
    row["payload"]["tags"].append("read-mutation")
    values = row.to_dict()
    values["payload"]["tags"].append("dict-mutation")
    assert row["payload"] == {"tags": ["original"]}
    source = {"payload": {"tags": ["input"]}}
    detached = QueryRow("detached", source)
    source["payload"]["tags"].append("after-construction")
    assert detached["payload"] == {"tags": ["input"]}
    assert query_store.snapshot_by_schema(
        "QueryArticleSchema", id="article-one", key="payload"
    ) == {"tags": ["original"]}


@pytest.mark.parametrize("source", [None, 3, "", QueryRootSchema])
def test_query_requires_collection_source(source: Any) -> None:
    with pytest.raises(TypeError, match="Query source"):
        Query(source)


def test_duplicate_join_paths_and_aliases_are_rejected() -> None:
    query = Query(QueryArticleSchema).join("author_id", as_="author")
    with pytest.raises(ValueError, match="path .* already used"):
        query.join("author_id", as_="another")
    with pytest.raises(ValueError, match="alias .* already used"):
        query.join("reviewer_id", as_="author")
    with pytest.raises(ValueError, match="cannot contain dots"):
        Query(QueryArticleSchema).join("author_id", as_="with.dots")
    with pytest.raises(ValueError, match="non-empty"):
        Query(QueryArticleSchema).join("author_id", as_="")
