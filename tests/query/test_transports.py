from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from kivyfn.store import (
    HostConfig,
    Query,
    Store,
    StoreClient,
    StoreHost,
    field,
)
from kivyfn.store.protocol.message.responses import QueryResponse
from kivyfn.store.state.manager import StateManager
from kivyfn.store.state.transaction.transaction import StateTransaction
from tests.conftest import TransportKind
from tests.helpers import state_value
from tests.query.conftest import QueryArticleSchema, QueryAuthorSchema


@dataclass
class QueryStack:
    host: StoreHost
    client: StoreClient
    state: StateManager
    endpoint: str


@pytest_asyncio.fixture
async def query_stack(
    query_store: Store, transport_kind: TransportKind
) -> AsyncIterator[QueryStack]:
    endpoint = (
        f"direct://query-{uuid4().hex}"
        if transport_kind == "direct"
        else "http://127.0.0.1:0"
    )
    host = StoreHost(query_store, HostConfig(endpoint))
    client: StoreClient | None = None
    try:
        await host.start()
        if transport_kind == "socketio":
            endpoint = str(host.transport.endpoint["endpoint"])
        client = StoreClient()
        state = StateManager(client)
        yield QueryStack(host, client, state, endpoint)
    finally:
        if client is not None:
            await client.disconnect()
        await host.stop()


@pytest.mark.asyncio
async def test_nullable_query_stays_live_and_refreshes_after_dependency_change(
    query_stack: QueryStack,
) -> None:
    authors = query_stack.state.create(QueryAuthorSchema)
    joined = query_stack.state.create(
        Query(QueryArticleSchema)
        .left_join("author_id", as_="author")
        .where(field("author.score") > 18)
    )
    await query_stack.client.connect(query_stack.endpoint)
    assert joined.keys() == ("article-one",)
    await authors["author-a"].set(score=None)
    assert joined.keys() == ()
    assert joined.synced and query_stack.host.running
    await authors["author-a"].set(score=25)
    assert joined.keys() == ("article-one",)
    assert state_value(joined["article-one"], "author_score") == 25
    assert joined["article-one"].synced


@pytest.mark.asyncio
async def test_equivalent_class_and_name_queries_have_one_live_target(
    query_stack: QueryStack,
) -> None:
    first = query_stack.state.create(Query(QueryArticleSchema))
    second = query_stack.state.create(
        Query[QueryArticleSchema]("QueryArticleSchema")
    )
    assert first is second
    await query_stack.client.connect(query_stack.endpoint)
    articles = query_stack.state.create(QueryArticleSchema)
    # connect waits for incrementally created subscriptions too.
    await query_stack.client.connect(query_stack.endpoint)
    await articles["article-one"].set(title="Shared")
    assert state_value(first["article-one"], "title") == "Shared"
    assert first.synced and second.synced


@pytest.mark.asyncio
async def test_shared_query_row_signals_once_per_occurrence(
    query_stack: QueryStack,
) -> None:
    articles = query_stack.state.create(QueryArticleSchema)
    all_articles = query_stack.state.create(Query(QueryArticleSchema))
    published = query_stack.state.create(
        Query(QueryArticleSchema).where(
            field("published") == True  # noqa: E712
        )
    )
    await query_stack.client.connect(query_stack.endpoint)
    row = all_articles["article-one"]
    assert row is published["article-one"]
    seen: list[tuple[str, Any]] = []
    row.bind(on_alert=lambda _, data: seen.append(("row", data)))
    all_articles.bind(
        on_alert=lambda _, row_id, data: seen.append(("all", data))
    )
    published.bind(
        on_alert=lambda _, row_id, data: seen.append(("published", data))
    )
    with query_stack.state.transaction() as transaction:
        emitter: Any = articles["article-one"]
        emitter.alert("same")
        emitter.alert("same")
    await transaction
    assert seen.count(("row", "same")) == 2
    assert seen.count(("all", "same")) == 2
    assert seen.count(("published", "same")) == 2
    assert seen[0] == ("row", "same")


@pytest.mark.asyncio
async def test_query_signal_observes_final_joined_and_root_values(
    query_stack: QueryStack,
) -> None:
    articles = query_stack.state.create(QueryArticleSchema)
    authors = query_stack.state.create(QueryAuthorSchema)
    joined = query_stack.state.create(
        Query(QueryArticleSchema).join("author_id", as_="author")
    )
    await query_stack.client.connect(query_stack.endpoint)
    seen = []
    joined["article-one"].bind(
        on_alert=lambda _, data: seen.append(
            (
                state_value(joined["article-one"], "title"),
                state_value(joined["article-one"], "author_nickname"),
                state_value(authors["author-a"], "nickname"),
            )
        )
    )
    with query_stack.state.transaction() as transaction:
        emitter: Any = articles["article-one"]
        emitter.alert()
        articles["article-one"].set(title="Final")
        authors["author-a"].set(nickname="Final author")
    await transaction
    assert seen == [("Final", "Final author", "Final author")]


@pytest.mark.asyncio
async def test_release_query_then_recreate_hydrates_current_snapshot(
    query_stack: QueryStack,
) -> None:
    articles = query_stack.state.create(QueryArticleSchema)
    query = Query(QueryArticleSchema).where(
        field("published") == True  # noqa: E712
    )
    collection = query_stack.state.create(query)
    await query_stack.client.connect(query_stack.endpoint)
    row = collection["article-one"]
    query_stack.state.release(query)
    assert not collection.synced and not row.synced
    await articles["article-one"].set(title="While released")
    resumed = query_stack.state.create(query)
    await query_stack.client.connect(query_stack.endpoint)
    assert resumed.synced
    assert state_value(resumed["article-one"], "title") == "While released"
    assert resumed["article-one"].synced


@pytest.mark.asyncio
async def test_one_shot_nested_query_uses_both_transport_codecs(
    query_stack: QueryStack,
) -> None:
    await query_stack.client.connect(query_stack.endpoint)
    query = (
        Query(QueryArticleSchema)
        .join("author_id", as_="author")
        .left_join("author.country_id", as_="country")
        .where(
            (field("author.score") > 18)
            & field("author.country.label").in_(["North"])
        )
    )
    response = await query_stack.client.query(query)
    assert isinstance(response, QueryResponse)
    assert len(response.rows) == 1
    assert response.rows[0].id == "article-one"
    assert response.rows[0]["author_country_label"] == "North"


@pytest.mark.asyncio
async def test_callback_failure_does_not_break_other_query_signals(
    query_stack: QueryStack,
) -> None:
    articles = query_stack.state.create(QueryArticleSchema)
    all_articles = query_stack.state.create(Query(QueryArticleSchema))
    await query_stack.client.connect(query_stack.endpoint)
    received = asyncio.Event()

    def fail(*_: Any) -> None:
        raise RuntimeError("broken query row callback")

    all_articles["article-one"].bind(on_alert=fail)
    all_articles.bind(on_alert=lambda *_: received.set())
    emitter: Any = articles["article-one"]
    transaction: StateTransaction = emitter.alert()
    async with asyncio.timeout(2):
        await transaction
        await received.wait()
    assert transaction.done and transaction.success
    assert query_stack.client.synced and query_stack.host.running


@pytest.mark.asyncio
async def test_failed_query_disconnects_only_its_client(
    query_stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    articles = query_stack.state.create(QueryArticleSchema)
    await query_stack.client.connect(query_stack.endpoint)
    with query_stack.state.transaction() as transaction:
        for row in articles.values():
            row.set(payload=20)
    await transaction

    observer = StoreClient()
    manager = StateManager(observer)
    broken = manager.create(
        Query(QueryArticleSchema).where(field("payload") > 10)
    )
    disconnected = asyncio.Event()
    original = observer.on_disconnect

    def on_disconnect() -> None:
        original()
        disconnected.set()

    monkeypatch.setattr(observer, "on_disconnect", on_disconnect)
    try:
        await observer.connect(query_stack.endpoint)
        assert len(broken) == 3
        async with asyncio.timeout(2):
            await articles["article-one"].set(payload="incomparable")
            await disconnected.wait()
        assert not observer.online and not broken.synced
        assert query_stack.client.synced and query_stack.host.running
        await articles["article-one"].set(title="Host still serves writes")
        assert state_value(articles["article-one"], "title") == (
            "Host still serves writes"
        )
    finally:
        await observer.disconnect()
