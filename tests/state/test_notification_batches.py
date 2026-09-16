"""Update ownership and callback ordering across the shared reactive bases."""

from __future__ import annotations

import pytest

from kivyfn.store.error import StoreStateError
from kivyfn.store.query import Query, QueryRow
from kivyfn.store.state.reactive.base import BaseCollection
from kivyfn.store.state.reactive.collection import CollectionState
from kivyfn.store.state.reactive.dispatch import (
    collect_notifications,
    notification_batch,
)
from tests.state.helpers import StateUnitRows, StateUnitSingle, manager_pair


def test_state_owns_overlay_notifications_and_preserves_newer_writes():
    manager, _ = manager_pair()
    state = manager.create(StateUnitSingle)
    seen = []
    state.bind(value=lambda _, value: seen.append(value))

    state.set_overlay("first", {"value": 1})
    state.set_overlay("second", {"value": 2})
    state.set_canonical({"value": 3})
    state.discard_overlay("first")
    assert seen == [1, 2]
    assert state.value == 2

    state.discard_overlay("second")
    assert seen == [1, 2, 3]
    assert state.value == 3


def test_nested_updates_wait_for_explicit_publication():
    first, _ = manager_pair()
    second, _ = manager_pair()
    a = first.create(StateUnitSingle)
    b = second.create(StateUnitSingle)
    seen = []
    a.bind(value=lambda _, value: seen.append((value, b.value)))

    with collect_notifications() as batch:
        a.set_canonical({"value": 1})
        with notification_batch():
            a.set_canonical({"value": 2})
            b.set_canonical({"value": 3})
        assert seen == []
        assert (a.value, b.value) == (2, 3)

    assert seen == []
    batch.publish()
    assert seen == [(2, 3)]


def test_optimistic_insert_publishes_complete_row_then_membership():
    manager, client = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.enter("one")
    row = rows["one"]
    rows.leave("one")
    seen = []
    row.bind(value=lambda _, value: seen.append(("value", "one" in rows)))
    rows.bind(on_enter=lambda _, key, item: seen.append((key, item.value)))

    rows.apply_insert_overlay("insert", row, {"value": 7})
    assert rows["one"] is row
    assert seen == [("value", True), ("one", 7)]
    assert not client.requests

    rows.discard_overlay("insert")
    assert "one" not in rows


@pytest.mark.parametrize("query", [False, True])
def test_snapshot_callbacks_see_all_rows_and_preserve_mapping_view(query):
    manager, _ = manager_pair()
    rows = manager.create(Query(StateUnitRows) if query else StateUnitRows)
    assert isinstance(rows, BaseCollection)
    view = rows.rows
    seen = []
    rows.bind(
        on_enter=lambda _, key, row: seen.append(
            (key, row.value, rows["two"].value)
        )
    )
    rows.apply_snapshot(
        (QueryRow("one", {"value": 1}), QueryRow("two", {"value": 2}))
    )
    assert seen == [("one", 1, 2), ("two", 2, 2)]
    assert rows.rows is view
    assert len(rows) == 2
    assert tuple(rows) == ("one", "two")
    assert rows.get("one") is rows["one"]
    if query:
        assert not hasattr(rows, "insert")
        assert not hasattr(rows, "set_overlay")
        assert not hasattr(rows, "is_pending")
        assert not hasattr(rows, "pending_count")


@pytest.mark.asyncio
async def test_row_edit_does_not_mark_collection_as_transaction_participant():
    manager, client = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.enter("one")
    row = rows["one"]
    tx = row.set(value=4)
    assert row.is_pending
    assert not rows.is_pending
    await client.fail(tx)
    assert not row.is_pending
    assert not rows.is_pending


def test_shared_query_row_stays_synced_until_its_last_owner_releases():
    from kivyfn.store.query import field

    manager, _ = manager_pair()
    first_query = Query(StateUnitRows).where(field("value") > 0)
    second_query = Query(StateUnitRows).where(field("value") > 3)
    first = manager.create(first_query)
    second = manager.create(second_query)
    first.enter("shared")
    second.enter("shared")
    row = first["shared"]
    assert row is second["shared"]
    manager.on_subscription_status(first_query, True)
    manager.on_subscription_status(second_query, True)
    manager.release(first_query)
    assert row.synced
    manager.release(second_query)
    assert not row.synced


def test_optimistic_presence_does_not_keep_deleted_row_synced():
    manager, _ = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.enter("one")
    row = rows["one"]
    manager.on_subscription_status(StateUnitRows, True)
    rows.set_overlay("pending", {"one": row})
    assert row.synced
    rows.leave("one")
    assert rows["one"] is row
    assert not row.synced
    rows.discard_overlay("pending")
    assert "one" not in rows


@pytest.mark.parametrize("query", [False, True])
def test_row_updates_use_supplied_states_without_creating_rows(query):
    manager, _ = manager_pair()
    rows = manager.create(Query(StateUnitRows) if query else StateUnitRows)
    rows.enter("one")
    row = rows["one"]
    rows.leave("one")

    def unexpected_factory(row_id):
        raise AssertionError("A row update must use the supplied State")

    rows._row_factory = unexpected_factory
    rows.set_canonical({"one": row})
    assert rows["one"] is row
    rows.replace_canonical({"one": row})
    assert rows["one"] is row

    with pytest.raises(StoreStateError, match="row ID"):
        rows.set_canonical({"one": None, "wrong": row})
    assert rows["one"] is row

    if not query:
        assert isinstance(rows, CollectionState)
        rows.set_overlay("hide", {"one": None})
        assert "one" not in rows
        rows.set_overlay("show", {"one": row})
        assert rows["one"] is row
        with pytest.raises(StoreStateError, match="row ID"):
            rows.set_overlay("invalid", {"one": None, "wrong": row})
        assert rows["one"] is row
        rows.discard_overlay("show")
        assert "one" not in rows
        rows.discard_overlay("hide")
        assert rows["one"] is row


@pytest.mark.asyncio
async def test_query_row_owns_its_transaction_lifecycle():
    manager, client = manager_pair()
    rows = manager.create(Query(StateUnitRows))
    rows.enter("one")
    row = rows["one"]
    events = []
    row.bind(on_pending=lambda _, tx: events.append("pending"))
    row.bind(on_done=lambda _, tx, failure: events.append("done"))

    transaction = row.set(value=4)
    assert row.is_pending
    assert row.pending_count == 1
    assert "on_pending" not in type(rows).__events__
    assert not hasattr(rows, "pending_count")

    await client.fail(transaction)
    assert not row.is_pending
    assert row.pending_count == 0
    assert row.value == 0
    assert events == ["pending", "done"]
