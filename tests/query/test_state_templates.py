# tests/query/test_state_templates.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from kivy.properties import AliasProperty, BooleanProperty

from kivyfn.store import Query, field, state_template
from kivyfn.store.state.transaction.transaction import StateTransaction
from kivyfn.store.typing import CollectionStateType, QueryStateType, StateType
from tests.helpers import intercept_client_requests
from tests.query.conftest import QueryArticleSchema, QueryAuthorSchema
from tests.query.test_transports import QueryStack
from tests.query.test_transports import query_stack as query_stack


class ArticleState(StateType):
    title: str
    published: bool
    author_id: str | None
    payload: dict[str, Any]
    selected = BooleanProperty(False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.lifecycle: list[str] = []
        self.alerts: list[Any] = []
        super().__init__(*args, **kwargs)

    if TYPE_CHECKING:

        def alert(self, data: Any = None) -> StateTransaction: ...

    def on_alert(self, data: Any = None) -> None:
        self.alerts.append(data)

    def on_pending(self, transaction: StateTransaction) -> None:
        self.lifecycle.append("pending")

    def on_success(self, transaction: StateTransaction) -> None:
        self.lifecycle.append("success")

    def on_done(self, transaction: StateTransaction, failure: Any) -> None:
        self.lifecycle.append("done")

    def rename(self, title: str) -> StateTransaction:
        return self.set(title=title)


class JoinedArticleState(ArticleState):
    author_nickname: str | None
    author_score: int | float | None

    def _get_caption(self) -> str:
        return f"{self.title} by {self.author_nickname or 'Unknown'}"

    caption = AliasProperty(
        _get_caption, None, bind=("title", "author_nickname")
    )

    def rename_author(self, nickname: str) -> StateTransaction:
        return self.set(author_nickname=nickname)


class ArticleCollection(CollectionStateType[ArticleState]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.events: list[tuple[str, str, str]] = []
        super().__init__(*args, **kwargs)

    def on_enter(self, row_id: str, state: ArticleState) -> None:
        self.events.append(("enter", row_id, state.title))

    def on_leave(self, row_id: str, state: ArticleState) -> None:
        self.events.append(("leave", row_id, state.title))

    def on_insert(self, row_id: str, state: ArticleState) -> None:
        self.events.append(("insert", row_id, state.title))

    def on_remove(self, row_id: str, state: ArticleState) -> None:
        self.events.append(("remove", row_id, state.title))


class ArticleQuery(QueryStateType[JoinedArticleState]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.events: list[tuple[str, str, str | None]] = []
        self.alerts: list[tuple[str, Any]] = []
        super().__init__(*args, **kwargs)

    def on_enter(self, row_id: str, state: JoinedArticleState) -> None:
        self.events.append(("enter", row_id, state.author_nickname))

    def on_leave(self, row_id: str, state: JoinedArticleState) -> None:
        self.events.append(("leave", row_id, state.author_nickname))

    def on_alert(self, row_id: str, data: Any = None) -> None:
        self.alerts.append((row_id, data))

    def titles(self) -> tuple[str, ...]:
        return tuple(row.title for row in self.rows.values())


articles_template = state_template(
    QueryArticleSchema,
    state_type=ArticleState,
    collection_type=ArticleCollection,
)
joined_source = Query(QueryArticleSchema).left_join("author_id", as_="author")
joined_template = state_template(
    joined_source, state_type=JoinedArticleState, collection_type=ArticleQuery
)


@pytest.mark.asyncio
async def test_custom_query_snapshot_and_projected_writes(
    query_stack: QueryStack,
):
    articles = query_stack.state.create(articles_template)
    authors = query_stack.state.create(QueryAuthorSchema)
    joined = query_stack.state.create(joined_template)
    await query_stack.client.connect(query_stack.endpoint)
    assert isinstance(articles, ArticleCollection)
    assert isinstance(joined, ArticleQuery)
    assert joined.events == [
        ("enter", "article-one", "Ada"),
        ("enter", "article-two", "Bea"),
        ("enter", "article-orphan", None),
    ]
    row = joined["article-one"]
    assert isinstance(row, JoinedArticleState)
    assert row.caption == "First by Ada"
    assert row.schema.name == "QueryArticleSchema"
    observed = []
    row.bind(caption=lambda _, value: observed.append(value))
    tx = await row.rename_author("New author")
    assert tx.success
    assert authors["author-a"].get("nickname") == "New author"
    assert observed == ["First by New author"]
    assert row.lifecycle == ["pending", "success", "done"]
    assert joined["article-orphan"].author_nickname is None
    assert joined["article-orphan"].caption == "Orphan by Unknown"
    with pytest.raises(ValueError, match="has no target row"):
        joined["article-orphan"].rename_author("No owner")
    changed = await row.set(author_id="author-b", author_nickname="Retargeted")
    assert changed.success
    assert authors["author-b"].get("nickname") == "Retargeted"
    assert row.author_nickname == "Retargeted"
    assert articles["article-one"].author_id == "author-b"


@pytest.mark.asyncio
async def test_custom_membership_events_and_query_reentry(
    query_stack: QueryStack,
):
    articles = query_stack.state.create(articles_template)
    published_source = joined_source.where(field("published") == True)  # noqa: E712
    published = query_stack.state.create(
        state_template(
            published_source,
            state_type=JoinedArticleState,
            collection_type=ArticleQuery,
        )
    )
    joined = query_stack.state.create(joined_template)
    await query_stack.client.connect(query_stack.endpoint)
    row = published["article-one"]
    assert row is joined["article-one"]
    row.selected = True
    published.events.clear()
    await articles["article-one"].set(published=False)
    assert "article-one" not in published
    assert row.synced
    await articles["article-one"].set(published=True)
    assert published["article-one"] is row
    assert row.selected
    assert published.events == [
        ("leave", "article-one", "Ada"),
        ("enter", "article-one", "Ada"),
    ]
    articles.events.clear()
    tx = await articles.insert("added", title="Added")
    assert tx.success
    inserted = articles["added"]
    assert isinstance(inserted, ArticleState)
    assert articles.events == [
        ("enter", "added", "Added"),
        ("insert", "added", "Added"),
    ]
    await articles.remove("added")
    assert articles.events[-2:] == [
        ("leave", "added", "Added"),
        ("remove", "added", "Added"),
    ]


@pytest.mark.asyncio
async def test_custom_state_overlaps_failure_and_local_property_ownership(
    query_stack: QueryStack, monkeypatch: pytest.MonkeyPatch
):
    articles = query_stack.state.create(articles_template)
    requests = intercept_client_requests(monkeypatch, query_stack.client)
    await query_stack.client.connect(query_stack.endpoint)
    gate = asyncio.Event()
    submitted = asyncio.Event()

    async def blocked(batch):
        submitted.set()
        await gate.wait()
        return await requests.send(batch)

    requests.request = blocked
    row = articles["article-one"]
    row.selected = True
    with query_stack.state.transaction() as failing:
        row.title = "Must roll back"
        # The canonical owner checks whether this foreign-key ID exists.
        row.set(author_id="missing-author")
    async with asyncio.timeout(2):
        await submitted.wait()
        assert row.title == "Must roll back"
        strict = row.rename("Final title")
        assert row.title == "Must roll back"
        gate.set()
        await failing
        await strict
    assert failing.failed and strict.success
    assert row.title == "Final title"
    assert row.selected
    assert not row.is_pending
    assert row.pending_count == 0
    assert "selected" not in row.canonical_snapshot(row._state_field_names)


@pytest.mark.asyncio
async def test_custom_state_disconnect_reconnect_and_release(
    query_stack: QueryStack,
):
    articles = query_stack.state.create(articles_template)
    joined = query_stack.state.create(joined_template)
    await query_stack.client.connect(query_stack.endpoint)
    row = joined["article-one"]
    row.selected = True
    await query_stack.client.disconnect()
    assert not row.synced
    assert not articles.synced
    await query_stack.client.connect()
    assert query_stack.state.create(joined_template) is joined
    assert joined["article-one"] is row
    assert row.synced and row.selected
    query_stack.state.release(joined_template)
    assert not joined.synced
    await articles["article-one"].rename("While released")
    assert query_stack.state.create(joined_template) is joined
    await query_stack.client.connect()
    assert joined["article-one"] is row
    assert row.title == "While released"
    assert row.selected and row.synced


@pytest.mark.asyncio
async def test_custom_signal_handlers_observe_final_values_and_fail_safely(
    query_stack: QueryStack,
):
    events = []

    class SignalRow(JoinedArticleState):
        def on_alert(self, data: Any = None) -> None:
            events.append(("row", self.title, self.author_nickname, data))
            raise RuntimeError("custom row handler failed")

    class SignalQuery(ArticleQuery):
        def on_alert(self, row_id: str, data: Any = None) -> None:
            events.append(("query", row_id, self[row_id].title, data))

    articles = query_stack.state.create(articles_template)
    joined = query_stack.state.create(
        state_template(
            joined_source, state_type=SignalRow, collection_type=SignalQuery
        )
    )
    await query_stack.client.connect(query_stack.endpoint)
    with query_stack.state.transaction() as transaction:
        articles["article-one"].alert("same")
        joined["article-one"].set(title="Final", author_nickname="Updated")
        articles["article-one"].alert("same")
    await transaction
    assert transaction.success
    assert (
        events
        == [
            ("row", "Final", "Updated", "same"),
            ("query", "article-one", "Final", "same"),
        ]
        * 2
    )
    assert not joined["article-one"].is_pending
    assert query_stack.client.synced
    events.clear()
    with query_stack.state.transaction() as failed:
        articles["article-one"].alert("suppressed")
        articles["article-one"].set(author_id="missing")
    await failed
    assert failed.failed
    assert events == []


@pytest.mark.asyncio
async def test_custom_membership_callback_failure_keeps_transaction_progress(
    query_stack: QueryStack,
):
    class Broken(ArticleCollection):
        def on_enter(self, row_id: str, state: ArticleState) -> None:
            super().on_enter(row_id, state)
            raise RuntimeError("custom enter failed")

    articles = query_stack.state.create(
        state_template(
            QueryArticleSchema, state_type=ArticleState, collection_type=Broken
        )
    )
    await query_stack.client.connect(query_stack.endpoint)
    assert articles.synced
    inserted = await articles.insert("added", title="Added")
    assert inserted.success
    assert articles.events[-2:] == [
        ("enter", "added", "Added"),
        ("insert", "added", "Added"),
    ]
    assert not articles.is_pending
    assert (await articles["added"].rename("Still works")).success
