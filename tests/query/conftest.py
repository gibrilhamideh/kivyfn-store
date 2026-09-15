from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kivyfn.store import Store
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.events import StoreCommit
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    RequestAction,
    SetRowRequestAction,
)
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.fields import (
    BooleanField,
    DataField,
    ForeignKeyField,
    NumericField,
    StringField,
)
from kivyfn.store.schema.signal import Signal


class QueryRootSchema(SingleSchema):
    label = StringField(default="root")


class QueryCountrySchema(ManySchema):
    label = StringField(default="")


class QueryAuthorSchema(ManySchema):
    nickname = StringField(default="")
    score = NumericField(default=None, allow_none=True)
    country_id = ForeignKeyField("QueryCountrySchema")


class QueryArticleSchema(ManySchema):
    title = StringField(default="")
    published = BooleanField(default=True)
    author_id = ForeignKeyField("QueryAuthorSchema")
    reviewer_id = ForeignKeyField("QueryAuthorSchema")
    payload = DataField[dict[str, Any]](default={})
    alert = Signal[Any]()


def execute(store: Store, *actions: RequestAction) -> StoreCommit:
    result = store.execute(tuple(actions), transaction_id=uuid4().hex)
    assert result.success, result.error
    assert result.commit is not None
    return result.commit


def insert(schema: type[ManySchema], row_id: str, **values: Any):
    return InsertRequestAction(schema.__name__, row_id, tuple(values.items()))


def update(schema: type[ManySchema], row_id: str, **values: Any):
    return SetRowRequestAction(schema.__name__, row_id, tuple(values.items()))


@pytest.fixture
def query_store(tmp_path: Path) -> Iterator[Store]:
    tree = (
        Branch(QueryRootSchema)
        .child("countries", Branch(QueryCountrySchema))
        .child("authors", Branch(QueryAuthorSchema))
        .child("articles", Branch(QueryArticleSchema))
    )
    root, config = (
        Builder()
        .tree(tree)
        .storage_path(tmp_path)
        .boot_policy("raise")
        .build()
    )
    store = Store(root, config)
    store.boot()
    execute(
        store,
        insert(QueryCountrySchema, "country-x", label="North"),
        insert(QueryCountrySchema, "country-y", label="South"),
        insert(
            QueryAuthorSchema,
            "author-a",
            nickname="Ada",
            score=30,
            country_id="country-x",
        ),
        insert(QueryAuthorSchema, "author-b", nickname="Bea", score=None),
        insert(
            QueryAuthorSchema,
            "author-c",
            nickname="Cy",
            score=15,
            country_id="country-y",
        ),
        insert(
            QueryArticleSchema,
            "article-one",
            title="First",
            author_id="author-a",
            reviewer_id="author-c",
            payload={"tags": ["original"]},
        ),
        insert(
            QueryArticleSchema,
            "article-two",
            title="Second",
            author_id="author-b",
            published=False,
        ),
        insert(QueryArticleSchema, "article-orphan", title="Orphan"),
    )
    try:
        yield store
    finally:
        store.shutdown()
