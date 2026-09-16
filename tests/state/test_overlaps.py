from __future__ import annotations

import pytest

from kivyfn.store.client.subscription import SubscriptionUpdate
from kivyfn.store.protocol.message.event import SetEventAction
from kivyfn.store.protocol.message.requests import SetRequestAction
from tests.state.helpers import StateUnitSingle, manager_pair


@pytest.mark.asyncio
async def test_failed_earlier_overlay_keeps_later_optimistic_value():
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    with manager.transaction() as first:
        state.value = 1
    with manager.transaction() as second:
        state.value = 2
    assert state.pending_count == 2
    await client.fail(first)
    assert state.get("value") == 2
    assert state.pending_count == 1
    await client.fail(second)
    assert state.get("value") == 0
    assert state.pending_count == 0


@pytest.mark.asyncio
async def test_later_strict_commit_is_retained_under_earlier_overlay():
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    with manager.transaction() as first:
        state.value = 1
    second = state.set(value=2)
    assert state.get("value") == 1
    await client.succeed(
        manager,
        second,
        SubscriptionUpdate(
            StateUnitSingle, (SetEventAction((("value", 2),)),)
        ),
    )
    assert state.get("value") == 1
    await client.fail(first)
    assert state.get("value") == 2


@pytest.mark.asyncio
async def test_strict_replacement_clears_only_its_field_overlay():
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    with manager.transaction() as tx:
        state.value = 1
        state.other = 2
        state.set(value=3)
        assert state.get("value") == 0
        assert state.get("other") == 2
    action = client.requests[tx.id][0]
    assert isinstance(action, SetRequestAction)
    values = dict(action.values)
    assert values == {"value": 3, "other": 2}
    await client.fail(tx)


@pytest.mark.asyncio
async def test_committed_overlay_removed_before_signal_callback():
    from kivyfn.store.protocol.message.event import SignalEventAction

    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    observed = []
    state.bind(on_ping=lambda *_: observed.append(state.get("value")))
    with manager.transaction() as tx:
        state.value = 2
        state.ping()
    # The canonical transaction may transform the proposed optimistic value.
    await client.succeed(
        manager,
        tx,
        SubscriptionUpdate(
            StateUnitSingle,
            (SignalEventAction("ping"), SetEventAction((("value", 3),))),
        ),
    )
    assert observed == [3]
    assert state.get("value") == 3


@pytest.mark.asyncio
async def test_adjacent_singleton_sets_still_coalesce_to_latest_values():
    manager, client = manager_pair()
    state = manager.create(StateUnitSingle)
    with manager.transaction() as tx:
        state.set(value=1, other=2)
        state.set(value=3)
    assert len(client.requests[tx.id]) == 1
    action = client.requests[tx.id][0]
    assert isinstance(action, SetRequestAction)
    assert dict(action.values) == {"value": 3, "other": 2}
    await client.fail(tx)
