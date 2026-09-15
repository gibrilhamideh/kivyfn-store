from __future__ import annotations

import pytest

from kivyfn.store.protocol.message.event import (
    InsertEventAction,
    RemoveEventAction,
    SetRowEventAction,
    SignalRowEventAction,
    SubscriptionEvent,
)
from kivyfn.store.protocol.message.requests import InsertRequestAction
from tests.state.helpers import StateUnitRows, manager_pair


def event(*actions):
    return SubscriptionEvent("rows", {}, actions)


def rows_pair():
    manager, client = manager_pair()
    rows = manager.create(StateUnitRows)
    manager.bind_subscription(StateUnitRows, "rows")
    return manager, client, rows


@pytest.mark.asyncio
async def test_failed_second_strict_insert_preserves_canonical_first() -> None:
    manager, client, rows = rows_pair()
    first = rows.insert("x", value=1)
    second = rows.insert("x", value=2)
    await client.succeed(
        manager, first, event(InsertEventAction("x", (("value", 1),)))
    )
    row = rows["x"]
    await client.fail(second)
    assert rows["x"] is row
    assert row.get("value") == 1


@pytest.mark.asyncio
async def test_failed_second_strict_remove_does_not_resurrect_row() -> None:
    manager, client, rows = rows_pair()
    rows.enter("x")
    first = rows.remove("x")
    second = rows.remove("x")
    await client.succeed(manager, first, event(RemoveEventAction("x")))
    await client.fail(second)
    assert "x" not in rows


@pytest.mark.asyncio
async def test_failed_optimistic_remove_preserves_remote_delete() -> None:
    manager, client, rows = rows_pair()
    rows.enter("x")
    tx = manager.transaction()
    with tx:
        del rows["x"]
    manager.on_subscription_event(event(RemoveEventAction("x")))
    await client.fail(tx)
    assert "x" not in rows


@pytest.mark.asyncio
async def test_hidden_row_receives_update_and_signal_before_remove_failure():
    manager, client, rows = rows_pair()
    rows.enter("x")
    row = rows["x"]
    received = []
    row.bind(on_ping=lambda _, data: received.append(row.get("value")))
    tx = manager.transaction()
    with tx:
        del rows["x"]
    manager.on_subscription_event(
        event(
            SetRowEventAction("x", (("value", 7),)),
            SignalRowEventAction("x", "ping"),
        )
    )
    assert "x" not in rows
    assert received == [7]
    await client.fail(tx)
    assert rows["x"] is row
    assert row.get("value") == 7


@pytest.mark.asyncio
async def test_snapshot_updates_hidden_canonical_membership() -> None:
    manager, client, rows = rows_pair()
    rows.enter("x")
    tx = manager.transaction()
    with tx:
        del rows["x"]
    rows.set_membership(("y",))
    await client.fail(tx)
    assert rows.keys() == ("y",)


@pytest.mark.asyncio
async def test_remove_insert_remove_keeps_final_removal_overlay() -> None:
    manager, client, rows = rows_pair()
    rows.enter("x")
    tx = manager.transaction()
    with tx:
        del rows["x"]
        rows["x"] = {"value": 2}
        del rows["x"]
        assert "x" not in rows
    await client.succeed(manager, tx, event(RemoveEventAction("x")))
    assert "x" not in rows


@pytest.mark.asyncio
async def test_body_exception_restores_identity_values_and_membership():
    manager, client, rows = rows_pair()
    rows.enter("x")
    row = rows["x"]
    tx = manager.transaction()
    with pytest.raises(ValueError, match="body"):
        with tx:
            row.value = 4
            rows["new"] = {"value": 5}
            del rows["x"]
            raise ValueError("body")
    await tx
    assert tx.failed
    assert rows["x"] is row
    assert row.get("value") == 0
    assert "new" not in rows
    assert not client.requests


@pytest.mark.asyncio
async def test_defaults_only_insert_is_valid_and_owned() -> None:
    manager, client, rows = rows_pair()
    tx = rows.insert("x")
    action = client.requests[tx.id][0]
    assert isinstance(action, InsertRequestAction)
    assert dict(action.values)["value"] == 0
    await client.succeed(manager, tx, event(InsertEventAction("x", ())))
    assert rows["x"].get("payload") == {}


def test_membership_overlay_precedence_and_read_only_mapping() -> None:
    _, _, rows = rows_pair()
    rows.set_membership(("a", "b"))
    first = rows["a"]
    rows.set_membership_overlay("first", "a", False)
    rows.set_membership_overlay("second", "a", True)
    assert rows["a"] is first
    rows.leave("a")
    rows.discard_membership_overlay("first")
    assert rows["a"] is first
    rows.discard_membership_overlay("second")
    assert rows.keys() == ("b",)
    with pytest.raises(TypeError):
        rows.rows["x"] = first
