from __future__ import annotations

import pytest

from kivyfn.store.protocol.message.event import (
    EventMessage,
    SignalRowEventAction,
    SubscriptionEvent,
)
from kivyfn.store.query import Query, field
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.fields import ForeignKeyField, StringField
from kivyfn.store.schema.signal import Signal
from tests.state.helpers import manager_pair


class StateUnitCountry(ManySchema):
    label = StringField(default="")


class StateUnitAuthor(ManySchema):
    label = StringField(default="")
    country_id = ForeignKeyField(schema="StateUnitCountry")


class StateUnitPost(ManySchema):
    label = StringField(default="")
    author_id = ForeignKeyField(schema="StateUnitAuthor")
    ping = Signal()


def query_pair(*, nested=False):
    manager, client = manager_pair()
    query = Query(StateUnitPost).left_join("author_id", as_="author")
    if nested:
        query = query.left_join("author.country_id", as_="country")
    posts = manager.create(query)
    posts.enter("p")
    row = posts["p"]
    values = {
        "label": "post",
        "author_id": "a",
        "author_label": "old",
        "author_country_id": "c",
    }
    if nested:
        values["author_country_label"] = "country"
    row.set_canonical(values)
    return manager, client, posts, row


@pytest.mark.asyncio
async def test_held_query_row_uses_shape_after_leaving_membership():
    manager, client, posts, row = query_pair()
    posts.leave("p")
    tx = row.set(author_label="new")
    action = client.row_writes(tx)[0]
    assert action.schema == "StateUnitAuthor"
    assert action.row_id == "a"
    assert dict(action.values) == {"label": "new"}
    await client.fail(tx)


@pytest.mark.asyncio
async def test_other_transaction_fk_overlay_does_not_redirect_joined_write():
    manager, client, _, row = query_pair()
    with manager.transaction() as pending:
        row.author_id = "b"
    strict = row.set(author_label="new")
    assert client.row_writes(strict)[0].row_id == "a"
    await client.fail(strict)
    await client.fail(pending)


@pytest.mark.asyncio
async def test_strict_fk_and_joined_write_use_same_transaction_target():
    manager, client, _, row = query_pair()
    tx = row.set(author_label="new", author_id="b")
    writes = {a.schema: a for a in client.row_writes(tx)}
    assert dict(writes["StateUnitPost"].values) == {"author_id": "b"}
    assert writes["StateUnitAuthor"].row_id == "b"
    assert dict(writes["StateUnitAuthor"].values) == {"label": "new"}
    await client.fail(tx)


@pytest.mark.asyncio
async def test_sequential_retarget_keeps_both_projected_canonical_writes():
    manager, client, _, row = query_pair()
    with manager.transaction() as tx:
        row.set(author_label="first")
        row.set(author_id="b")
        row.set(author_label="second")
    actual = {
        (a.schema, a.row_id): dict(a.values) for a in client.row_writes(tx)
    }
    assert actual[("StateUnitAuthor", "a")] == {"label": "first"}
    assert actual[("StateUnitAuthor", "b")] == {"label": "second"}
    await client.fail(tx)


@pytest.mark.asyncio
async def test_nested_retarget_missing_canonical_target_rejects_guessing():
    manager, client, _, row = query_pair(nested=True)
    with pytest.raises(ValueError, match="Cannot resolve nested joined write"):
        row.set(author_id="b", author_country_label="new")
    assert row.get("author_id") == "a"
    assert not client.requests


@pytest.mark.asyncio
async def test_nested_retarget_uses_subscribed_new_owner_canonical_row():
    manager, client, _, row = query_pair(nested=True)
    authors = manager.create(StateUnitAuthor)
    authors.enter("b")
    authors["b"].set_canonical({"country_id": "other-country"})
    manager.on_subscription_status(StateUnitAuthor, True)
    tx = row.set(author_id="b", author_country_label="new")
    countries = [
        action
        for action in client.row_writes(tx)
        if action.schema == "StateUnitCountry"
    ]
    assert countries[0].row_id == "other-country"
    await client.fail(tx)


@pytest.mark.asyncio
async def test_null_relationship_cannot_receive_projected_write():
    _, client, _, row = query_pair()
    row.set_canonical({"author_id": None})
    with pytest.raises(ValueError, match="has no target row"):
        row.set(author_label="new")
    assert not client.requests


def test_shared_query_row_signal_occurs_once_per_commit_occurrence():
    manager, _ = manager_pair()
    first_source = Query(StateUnitPost).where(field("label") == "x")
    second_source = Query(StateUnitPost).where(field("label") != "y")
    first = manager.create(first_source)
    second = manager.create(second_source)
    manager.bind_subscription(first_source, "first")
    manager.bind_subscription(second_source, "second")
    first.enter("p")
    second.enter("p")
    row = first["p"]
    assert row is second["p"]
    observed = []
    row.bind(on_ping=lambda *_: observed.append("row"))
    first.bind(on_ping=lambda *_: observed.append("first"))
    second.bind(on_ping=lambda *_: observed.append("second"))
    actions = tuple(
        SignalRowEventAction("p", "ping", {"x": []}, index) for index in (0, 1)
    )
    manager.on_subscription_message(
        EventMessage(
            "test",
            (
                SubscriptionEvent("first", {}, actions),
                SubscriptionEvent("second", {}, actions),
            ),
        )
    )
    assert observed.count("row") == 2
    assert observed.count("first") == observed.count("second") == 2


def test_shared_query_row_stays_synced_while_one_owner_remains():
    manager, _ = manager_pair()
    source = Query(StateUnitPost)
    other_source = source.where(field("label") == "x")
    first = manager.create(source)
    second = manager.create(other_source)
    first.enter("p")
    second.enter("p")
    manager.on_subscription_status(source, True)
    manager.on_subscription_status(other_source, True)
    row = first["p"]
    first.leave("p")
    assert row.synced
    manager.release(other_source)
    assert not row.synced


@pytest.mark.asyncio
async def test_interleaved_projected_and_canonical_writes_keep_call_order():
    manager, client, _, row = query_pair()
    authors = manager.create(StateUnitAuthor)
    authors.enter("a")
    with manager.transaction() as tx:
        row.set(author_label="first")
        authors["a"].set(label="second")
        row.set(author_label="third")
    labels = [dict(action.values)["label"] for action in client.row_writes(tx)]
    assert labels == ["first", "second", "third"]
    await client.fail(tx)
