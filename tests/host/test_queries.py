from __future__ import annotations

from dataclasses import replace

import pytest

from kivyfn.store import Query, Store, field
from kivyfn.store.address import Address
from kivyfn.store.error import InvalidOperationError, StoreStateError
from kivyfn.store.host.query import HostQueries, HostQuery
from kivyfn.store.host.subscription import HostSubscriptions
from kivyfn.store.protocol.message.event import (
    InsertEventAction,
    RemoveEventAction,
    SetRowEventAction,
    SignalRowEventAction,
)
from kivyfn.store.protocol.message.requests import (
    CollectionSubscriptionRequest,
    QuerySubscriptionRequest,
    RemoveRequestAction,
    SignalRowRequestAction,
    SingleSubscriptionRequest,
)
from tests.query.conftest import (
    QueryArticleSchema,
    QueryAuthorSchema,
    execute,
    insert,
    update,
)


def test_equivalent_queries_share_capture_and_reference_lifetime(
    query_store: Store,
) -> None:
    queries = HostQueries(query_store)
    first = queries.acquire(Query(QueryArticleSchema))
    second = queries.acquire(Query("QueryArticleSchema"))
    assert first is second
    assert first.references == 2
    assert len(queries) == 1
    queries.release(first)
    assert first.references == 1
    queries.release(second)
    assert len(queries) == 0
    queries.release(first)
    assert first.references == 0
    replacement = queries.acquire(Query(QueryArticleSchema))
    assert replacement is not first
    queries.clear()
    queries.release(replacement)
    assert len(queries) == 0


def test_live_query_rejects_address_without_registering(
    query_store: Store,
) -> None:
    queries = HostQueries(query_store)
    with pytest.raises(InvalidOperationError, match="schema name or class"):
        queries.acquire(Query(Address("articles")))
    assert len(queries) == 0


def test_query_dependency_revision_only_diff_and_later_membership(
    query_store: Store,
) -> None:
    query = (
        Query(QueryArticleSchema)
        .join("author_id", as_="author")
        .where(field("author.score") > 100)
    )
    live = HostQueries(query_store).acquire(query)
    initial = live.revisions
    commit = execute(
        query_store, update(QueryAuthorSchema, "author-a", nickname="Renamed")
    )
    assert live.capture(commit.schema_revisions) == ()
    assert (
        live.revisions["QueryArticleSchema"] == initial["QueryArticleSchema"]
    )
    assert live.revisions["QueryAuthorSchema"] == (
        initial["QueryAuthorSchema"] + 1
    )
    assert live.capture({"QueryCountrySchema": 99}) is None
    commit = execute(
        query_store, update(QueryAuthorSchema, "author-a", score=150)
    )
    captured = live.capture(commit.schema_revisions)
    assert captured is not None and len(captured) == 1
    assert isinstance(captured[0], InsertEventAction)
    assert captured[0].row_id == "article-one"
    assert [row.id for row in live.rows] == ["article-one"]


def test_query_diff_handles_insert_set_remove_and_empty_result(
    query_store: Store,
) -> None:
    live = HostQueries(query_store).acquire(
        Query(QueryArticleSchema).where(field("title") == "Only")
    )
    commit = execute(
        query_store, insert(QueryArticleSchema, "new", title="Only")
    )
    actions = live.capture(commit.schema_revisions)
    assert actions is not None and isinstance(actions[0], InsertEventAction)
    commit = execute(
        query_store, update(QueryArticleSchema, "new", payload={"x": [1]})
    )
    actions = live.capture(commit.schema_revisions)
    assert actions is not None and isinstance(actions[0], SetRowEventAction)
    commit = execute(
        query_store, RemoveRequestAction("QueryArticleSchema", "new")
    )
    assert live.capture(commit.schema_revisions) == (
        RemoveEventAction(row_id="new"),
    )
    assert live.rows == ()


@pytest.mark.parametrize("offset", [-1, 0, 2])
def test_live_query_rejects_noncontiguous_revision_without_mutating(
    query_store: Store, offset: int
) -> None:
    live = HostQueries(query_store).acquire(Query(QueryArticleSchema))
    initial = live.revisions
    with pytest.raises(StoreStateError, match="Query revision"):
        live.capture(
            {"QueryArticleSchema": initial["QueryArticleSchema"] + offset}
        )
    assert live.revisions == initial


def test_live_query_rows_and_revisions_do_not_expose_cache_mutation(
    query_store: Store,
) -> None:
    live = HostQueries(query_store).acquire(Query(QueryArticleSchema))
    live.rows[0]["payload"]["tags"].append("corrupt")
    live.revisions["QueryArticleSchema"] = 99
    assert live.rows[0]["payload"] == {"tags": ["original"]}
    assert live.revisions == {
        "QueryArticleSchema": query_store.schema_revisions[
            "QueryArticleSchema"
        ]
    }


def test_subscriptions_deduplicate_per_client_and_protect_ownership(
    query_store: Store,
) -> None:
    queries = HostQueries(query_store)
    subscriptions = HostSubscriptions(query_store, queries)
    request = QuerySubscriptionRequest(Query(QueryArticleSchema))
    first = subscriptions.subscribe_query("a", request)
    duplicate = subscriptions.subscribe_query("a", request)
    other = subscriptions.subscribe_query("b", request)
    assert first.subscription_id == duplicate.subscription_id
    assert other.subscription_id != first.subscription_id
    assert len(subscriptions) == 2 and len(queries) == 1
    held = subscriptions.get("a", first.subscription_id)
    assert held is not None and isinstance(held.source, HostQuery)
    assert held.source.references == 2
    assert subscriptions.release("b", first.subscription_id) is None
    assert subscriptions.get("b", first.subscription_id) is None
    subscriptions.release("a", first.subscription_id)
    assert held.source.references == 1
    assert len(subscriptions.for_client("a")) == 0
    subscriptions.remove_client("b")
    assert len(queries) == len(subscriptions) == 0


def test_cached_snapshot_requires_matching_store_and_all_revisions(
    query_store: Store,
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    query = Query(QueryArticleSchema).left_join("author_id", as_="author")
    first = subscriptions.subscribe_query("a", QuerySubscriptionRequest(query))
    assert first.rows is not None
    unchanged = subscriptions.subscribe_query(
        "a",
        QuerySubscriptionRequest(
            query, first.store_instance_id, first.revisions
        ),
    )
    assert unchanged.rows is None
    for instance, revisions in [
        ("old-instance", first.revisions),
        (first.store_instance_id, {"QueryArticleSchema": 1}),
        (first.store_instance_id, {**first.revisions, "unexpected": 0}),
    ]:
        response = subscriptions.subscribe_query(
            "a", QuerySubscriptionRequest(query, instance, revisions)
        )
        assert response.rows is not None


def test_schema_snapshot_cardinality_cache_and_cleanup(
    query_store: Store,
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    first = subscriptions.subscribe_schema(
        "a", CollectionSubscriptionRequest("QueryArticleSchema")
    )
    same = subscriptions.subscribe_schema(
        "a",
        CollectionSubscriptionRequest(
            "QueryArticleSchema", first.store_instance_id, first.revision
        ),
    )
    assert same.subscription_id == first.subscription_id
    assert same.rows is None  # type: ignore[union-attr]
    single = subscriptions.subscribe_schema(
        "a", SingleSubscriptionRequest("QueryRootSchema")
    )
    assert single.values == (("label", "root"),)  # type: ignore[union-attr]
    with pytest.raises(InvalidOperationError, match="not a SingleSchema"):
        subscriptions.subscribe_schema(
            "a", SingleSubscriptionRequest("QueryArticleSchema")
        )
    with pytest.raises(InvalidOperationError, match="not a ManySchema"):
        subscriptions.subscribe_schema(
            "a", CollectionSubscriptionRequest("QueryRootSchema")
        )
    assert len(subscriptions) == 2
    subscriptions.clear()
    assert len(subscriptions) == 0
    assert subscriptions.for_client("a") == ()


def test_capture_evaluates_shared_query_once_and_preserves_transaction(
    query_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    query = Query(QueryArticleSchema)
    first = subscriptions.subscribe_query("a", QuerySubscriptionRequest(query))
    subscriptions.subscribe_query("b", QuerySubscriptionRequest(query))
    held = subscriptions.get("a", first.subscription_id)
    assert held is not None and isinstance(held.source, HostQuery)
    live = held.source
    original = live._execute
    captures = []

    def counted():
        captures.append(True)
        return original()

    monkeypatch.setattr(live, "_execute", counted)
    commit = execute(
        query_store, update(QueryArticleSchema, "article-one", title="Changed")
    )
    messages = subscriptions.capture(commit)
    assert len(captures) == 1 and len(messages) == 2
    assert all(
        message.transaction_id == commit.transaction_id
        for _, message in messages
    )
    prepared = subscriptions.prepare("a", messages[0][1])
    assert prepared is not None
    assert prepared.transaction_id == commit.transaction_id
    subscriptions.release("a", first.subscription_id)
    assert subscriptions.prepare("a", messages[0][1]) is None


def test_query_capture_failure_isolated_to_affected_clients(
    query_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    bad = Query(QueryArticleSchema).where(field("title") == "First")
    response = subscriptions.subscribe_query(
        "bad-a", QuerySubscriptionRequest(bad)
    )
    subscriptions.subscribe_query("bad-b", QuerySubscriptionRequest(bad))
    subscriptions.subscribe_query(
        "good", QuerySubscriptionRequest(Query(QueryArticleSchema))
    )
    subscriptions.subscribe_schema(
        "bad-a", CollectionSubscriptionRequest("QueryArticleSchema")
    )
    held = subscriptions.get("bad-a", response.subscription_id)
    assert held is not None and isinstance(held.source, HostQuery)
    evaluations = []

    def fail():
        evaluations.append(True)
        raise RuntimeError("injected query capture failure")

    monkeypatch.setattr(held.source, "_execute", fail)
    failures = []
    commit = execute(
        query_store, update(QueryArticleSchema, "article-one", title="Changed")
    )
    messages = subscriptions.capture(
        commit, lambda client, error: failures.append((client, error))
    )
    assert len(evaluations) == 1
    assert {client for client, _ in failures} == {"bad-a", "bad-b"}
    assert [client for client, _ in messages] == ["good"]
    assert query_store.booted


@pytest.mark.parametrize("enter", [True, False])
def test_query_signal_filters_final_transaction_membership(
    query_store: Store, enter: bool
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    query = Query(QueryArticleSchema).where(
        field("published") == True  # noqa: E712
    )
    subscriptions.subscribe_query("a", QuerySubscriptionRequest(query))
    row_id = "article-two" if enter else "article-one"
    signal = SignalRowRequestAction(
        "QueryArticleSchema", row_id, "alert", "same"
    )
    commit = execute(
        query_store,
        signal,
        update(QueryArticleSchema, row_id, published=enter),
        signal,
    )
    messages = subscriptions.capture(commit)
    assert len(messages) == 1
    actions = messages[0][1].events[0].actions
    signals = [
        action
        for action in actions
        if isinstance(action, SignalRowEventAction)
    ]
    assert len(signals) == (2 if enter else 0)
    if enter:
        assert isinstance(actions[0], InsertEventAction)
        assert [action.occurrence_id for action in signals] == [0, 1]
    else:
        assert actions == (RemoveEventAction(row_id),)


def test_signal_identity_shared_across_schema_and_overlapping_queries(
    query_store: Store,
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    subscriptions.subscribe_schema(
        "a", CollectionSubscriptionRequest("QueryArticleSchema")
    )
    subscriptions.subscribe_query(
        "a", QuerySubscriptionRequest(Query(QueryArticleSchema))
    )
    subscriptions.subscribe_query(
        "a",
        QuerySubscriptionRequest(
            Query(QueryArticleSchema).where(field("title") == "First")
        ),
    )
    before = query_store.schema_revisions
    signal = SignalRowRequestAction(
        "QueryArticleSchema", "article-one", "alert"
    )
    commit = execute(query_store, signal, signal)
    messages = subscriptions.capture(commit)
    assert query_store.schema_revisions == before
    events = messages[0][1].events
    assert len(events) == 3
    for event in events:
        assert event.revisions == {}
        assert len(event.actions) == 2
        assert all(
            isinstance(action, SignalRowEventAction)
            for action in event.actions
        )
        assert [
            getattr(action, "occurrence_id") for action in event.actions
        ] == [0, 1]


def test_empty_query_filters_signal_without_spurious_event(
    query_store: Store,
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    subscriptions.subscribe_query(
        "a",
        QuerySubscriptionRequest(
            Query(QueryArticleSchema).where(field("title") == "missing")
        ),
    )
    commit = execute(
        query_store,
        SignalRowRequestAction("QueryArticleSchema", "article-one", "alert"),
    )
    assert subscriptions.capture(commit) == ()


def test_malformed_commit_and_wrong_store_message_rejected(
    query_store: Store,
) -> None:
    subscriptions = HostSubscriptions(query_store, HostQueries(query_store))
    subscriptions.subscribe_schema(
        "a", CollectionSubscriptionRequest("QueryArticleSchema")
    )
    commit = execute(
        query_store, update(QueryArticleSchema, "article-one", title="New")
    )
    with pytest.raises(StoreStateError, match="another Store"):
        subscriptions.capture(replace(commit, server_instance_id="wrong"))
    with pytest.raises(StoreStateError, match="revisions do not match"):
        subscriptions.capture(replace(commit, schema_revisions={}))
    message = subscriptions.capture(commit)[0][1]
    with pytest.raises(StoreStateError, match="another Store"):
        subscriptions.prepare("a", replace(message, store_instance_id="wrong"))
