from __future__ import annotations

import asyncio

import pytest

from kivyfn.store.protocol.message.event import (
    EventMessage,
    SetEventAction,
    SetRowEventAction,
    SignalEventAction,
    SubscriptionEvent,
)
from kivyfn.store.protocol.message.responses import TransactionResponse
from tests.state.helpers import StateUnitRows, StateUnitSingle, manager_pair


def broken(*args) -> None:
    raise ValueError("application callback failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["on_pending", "on_success", "on_done"])
async def test_state_callback_failure_does_not_strand_transaction(event):
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    state.bind(**{event: broken})
    state.bind(pending_count=broken, is_pending=broken)
    tx = state.set(value=1)
    await client.succeed(manager, tx)
    assert tx.success
    assert not state.is_pending
    assert state.pending_count == 0


@pytest.mark.asyncio
async def test_failure_callbacks_do_not_skip_other_participants() -> None:
    manager, client = manager_pair()
    first = manager.create(StateUnitSingle)
    rows = manager.create(StateUnitRows)
    rows.enter("x")
    row = rows["x"]
    first.bind(on_failed=broken, on_done=broken)
    failures = []
    row.bind(on_failed=lambda *_: failures.append("row"))
    tx = manager.transaction()
    with tx:
        first.value = 7
        row.value = 8
    await client.fail(tx)
    assert tx.failed
    assert failures == ["row"]
    assert first.get("value") == row.get("value") == 0
    assert not first.is_pending
    assert not row.is_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["on_pending", "on_success", "on_done"])
async def test_collection_callbacks_do_not_interrupt_completion(event):
    manager, client = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.bind(**{event: broken})
    tx = rows.insert("x")
    await client.succeed(manager, tx)
    assert tx.success
    assert rows.pending_count == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_transaction_or_other_waiter():
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    tx = state.set(value=1)
    cancelled = asyncio.create_task(tx.wait())
    other = asyncio.create_task(tx.wait())
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not tx.done
    await client.succeed(manager, tx)
    assert await other is tx
    assert tx.success


@pytest.mark.asyncio
async def test_cancelled_transport_future_completes_as_failure() -> None:
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    tx = state.set(value=1)
    client.futures[tx.id].cancel()
    await asyncio.wait_for(tx.wait(), 1)
    assert tx.failed
    assert not state.is_pending


@pytest.mark.asyncio
async def test_wrong_response_transaction_id_cleans_up_overlay() -> None:
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    with manager.transaction() as tx:
        state.value = 3
    client.futures[tx.id].set_result(TransactionResponse("wrong"))
    await tx
    assert tx.failed
    assert state.get("value") == 0
    assert state.pending_count == 0


@pytest.mark.asyncio
async def test_close_finishes_pending_work_and_releases_subscriptions():
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    manager.on_subscription_status(StateUnitSingle, True)
    with manager.transaction() as tx:
        state.value = 2
    manager.close()
    await tx
    assert tx.failed
    assert state.get("value") == 0
    assert not state.synced
    assert not client.sources
    manager.close()
    client.futures[tx.id].set_result(TransactionResponse(tx.id))


def test_rows_track_owning_subscription_sync_and_release() -> None:
    manager, _ = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.enter("x")
    row = rows["x"]
    assert not row.synced
    manager.on_subscription_status(StateUnitRows, True)
    assert row.synced
    manager.on_subscription_status(StateUnitRows, False)
    assert not row.synced
    manager.on_subscription_status(StateUnitRows, True)
    manager.release(StateUnitRows)
    assert not row.synced
    assert not rows.synced
    assert manager.create(StateUnitRows) is rows


@pytest.mark.asyncio
async def test_child_task_cannot_mutate_open_parent_transaction() -> None:
    manager, _ = manager_pair()
    state = manager.create(StateUnitSingle)

    async def mutate():
        state.value = 1

    with manager.transaction() as tx:
        task = asyncio.create_task(mutate())
        with pytest.raises(RuntimeError, match="another asyncio task"):
            await task
    await tx
    assert tx.success
    assert state.get("value") == 0


@pytest.mark.asyncio
async def test_inherited_closed_transaction_does_not_capture_child_work():
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    ready = asyncio.Event()

    async def mutate():
        await ready.wait()
        return state.set(value=1)

    task = None
    with manager.transaction() as outer:
        task = asyncio.create_task(mutate())
    await outer
    ready.set()
    assert task is not None
    tx = await task
    assert tx is not outer
    await client.succeed(manager, tx)


def test_all_fields_and_schemas_are_staged_before_bindings_and_signals():
    manager, _ = manager_pair()
    state = manager.create(StateUnitSingle)
    rows = manager.create(StateUnitRows)
    rows.enter("x")
    row = rows["x"]
    manager.bind_subscription(StateUnitSingle, "single")
    manager.bind_subscription(StateUnitRows, "rows")
    observed = []

    def observe(*args):
        observed.append((state.get("value"), state.get("other"), row.value))

    state.bind(value=observe, other=observe, on_ping=observe)
    manager.on_subscription_message(
        EventMessage(
            "test",
            (
                SubscriptionEvent(
                    "single",
                    {},
                    (
                        SignalEventAction("ping"),
                        SetEventAction((("value", 1), ("other", 2))),
                    ),
                ),
                SubscriptionEvent(
                    "rows", {}, (SetRowEventAction("x", (("value", 3),)),)
                ),
            ),
        )
    )
    assert observed == [(1, 2, 3)] * 3


def test_mutable_property_read_is_detached_from_authoritative_values():
    manager, _ = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.enter("x")
    row = rows["x"]
    row.set_canonical({"payload": {"nested": [1]}})
    row.publish({"payload"})
    row.payload["nested"].append(2)
    assert row.payload == {"nested": [1]}
    assert row.canonical_snapshot({"payload"}) == {"payload": {"nested": [1]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", [False, True])
async def test_pending_property_callbacks_read_coherent_flags(collection):
    manager, client = manager_pair()
    target = manager.create(StateUnitRows if collection else StateUnitSingle)
    observed = []
    target.bind(
        pending_count=lambda *_: observed.append(
            (target.pending_count, target.is_pending)
        )
    )
    if collection:
        tx = target.insert("x")
    else:
        tx = target.set(value=1)
    await client.succeed(manager, tx)
    assert observed == [(1, True), (0, False)]


def test_prepare_defers_sync_callbacks_until_all_canonical_values_are_ready():
    from kivyfn.store.protocol.message.event import InsertEventAction

    manager, _ = manager_pair()
    rows = manager.create(StateUnitRows)
    manager.bind_subscription(StateUnitRows, "rows")
    rows.enter("x")
    row = rows["x"]
    rows.leave("x")
    manager.on_subscription_status(StateUnitRows, True)
    observed = []
    row.bind(synced=lambda *_: observed.append(row.get("value")))
    publish = manager.prepare_subscription_message(
        EventMessage(
            "test",
            (
                SubscriptionEvent(
                    "rows", {}, (InsertEventAction("x", (("value", 4),)),)
                ),
            ),
        )
    )
    assert row.get("value") == 4
    assert observed == []
    publish()
    assert observed == [4]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connection", "timeout"])
async def test_transport_domain_errors_preserve_failure_category(kind):
    from kivyfn.store.error import StoreConnectionError, StoreTimeoutError

    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    tx = state.set(value=1)
    error = (
        StoreConnectionError("outcome unknown")
        if kind == "connection"
        else StoreTimeoutError("request timed out")
    )
    client.futures[tx.id].set_exception(error)
    await tx
    assert tx.failure is not None
    assert tx.failure.type == (
        "network" if kind == "connection" else "timeout"
    )
    assert not state.is_pending


@pytest.mark.asyncio
async def test_history_metadata_captured_when_state_transaction_is_created():
    manager, client = manager_pair()
    rows = manager.create(StateUnitRows)
    rows.enter("a")
    transaction = manager.transaction(
        history_group="gesture-1", history_label="Move component"
    )
    with pytest.raises(AttributeError):
        setattr(transaction, "history_group", "gesture-2")
    with transaction:
        rows["a"].value = 1
    assert client.metadata[transaction.id] == ("gesture-1", "Move component")
    await client.fail(transaction)
