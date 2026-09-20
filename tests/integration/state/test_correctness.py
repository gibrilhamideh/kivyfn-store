"""Canonical State contracts exercised over both configured transports."""

from __future__ import annotations

import pytest

from kivyfn.store.query import Query
from tests.conftest import StateStack
from tests.schemas.settings import UserSettingsSchema
from tests.schemas.users import UserSchema


async def seeded(stack: StateStack):
    users = stack.state.create(UserSchema)
    await stack.client.connect(stack.endpoint)
    await users.insert("a", username="first", email="a@example.com", age=10)
    await users.insert("b", username="second", email="b@example.com", age=20)
    return users


@pytest.mark.asyncio
async def test_pending_duplicate_insert_failure_preserves_first_commit(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)
    first = users.insert("a", email="a@example.com")
    second = users.insert("a", email="other@example.com")
    await first
    await second
    assert first.success
    assert second.failed
    assert users["a"].get("email") == "a@example.com"
    assert users.synced


@pytest.mark.asyncio
async def test_pending_duplicate_remove_failure_preserves_canonical_delete(
    state_stack: StateStack,
) -> None:
    users = await seeded(state_stack)
    row = users["a"]
    first = users.remove("a")
    second = users.remove("a")
    await first
    await second
    assert first.success
    assert second.failed
    assert "a" not in users
    assert not row.synced
    assert users.synced


@pytest.mark.asyncio
async def test_multirow_field_binding_observes_complete_canonical_commit(
    state_stack: StateStack,
) -> None:
    users = await seeded(state_stack)
    observed = []
    users["a"].bind(age=lambda *_: observed.append(users["b"].get("age")))
    with state_stack.state.transaction() as tx:
        users["a"].set(age=11)
        users["b"].set(age=21)
    await tx
    assert tx.success
    assert observed == [21]


@pytest.mark.asyncio
async def test_property_callback_failure_does_not_break_subscription_stream(
    state_stack: StateStack,
) -> None:
    users = await seeded(state_stack)

    def broken(*args):
        raise RuntimeError("application property callback")

    users["a"].bind(age=broken, on_success=broken, on_done=broken)
    first = users["a"].set(age=11)
    await first
    second = users["a"].set(age=12)
    await second
    assert first.success and second.success
    assert users["a"].get("age") == 12
    assert users.synced
    assert users["a"].synced
    assert not users["a"].is_pending


@pytest.mark.asyncio
async def test_strict_retarget_and_projected_write_change_new_related_row(
    state_stack: StateStack,
) -> None:
    settings = state_stack.state.create(UserSettingsSchema)
    joined = state_stack.state.create(
        Query(UserSettingsSchema).join("user_id", as_="user")
    )
    users = await seeded(state_stack)
    await settings.remove("b")
    tx = joined["a"].set(user_id="b", user_username="retargeted")
    await tx
    assert tx.success
    assert users["a"].get("username") == "first"
    assert users["b"].get("username") == "retargeted"
    assert joined["a"].get("user_id") == "b"


@pytest.mark.asyncio
async def test_two_projections_interleaved_writes_preserve_user_call_order(
    state_stack: StateStack,
) -> None:
    joined = state_stack.state.create(
        Query(UserSettingsSchema).join("user_id", as_="user")
    )
    users = await seeded(state_stack)
    with state_stack.state.transaction() as tx:
        joined["a"].set(user_username="first-write")
        users["a"].set(username="second-write")
        joined["a"].set(user_username="final-write")
    await tx
    assert tx.success
    assert users["a"].get("username") == "final-write"
    assert joined["a"].get("user_username") == "final-write"


@pytest.mark.asyncio
async def test_remove_reinsert_preserves_row_identity_and_new_values(
    state_stack: StateStack,
) -> None:
    users = await seeded(state_stack)
    row = users["a"]
    with state_stack.state.transaction() as tx:
        del users["a"]
        users["a"] = {"email": "new@example.com", "age": 7}
    await tx
    assert tx.success
    assert users["a"] is row
    assert row.get("email") == "new@example.com"
    assert row.get("age") == 7
    assert row.synced


@pytest.mark.asyncio
async def test_rows_resynchronize_after_disconnect_without_identity_change(
    state_stack: StateStack,
) -> None:
    users = await seeded(state_stack)
    row = users["a"]
    assert row.synced
    await state_stack.client.disconnect()
    assert not row.synced
    await state_stack.client.connect(state_stack.endpoint)
    assert users["a"] is row
    assert row.synced


@pytest.mark.asyncio
async def test_two_managers_stage_all_schemas_before_membership_callbacks(
    state_stack: StateStack,
) -> None:
    from kivyfn.store.state.manager import StateManager

    other = StateManager(state_stack.client)
    users = state_stack.state.create(UserSchema)
    settings = other.create(UserSettingsSchema)
    await state_stack.client.connect(state_stack.endpoint)
    observed = []
    users.bind(
        on_insert=lambda _, row_id, __: observed.append(
            (row_id in settings, settings[row_id].get("theme"))
        )
    )
    tx = users.insert("a", email="a@example.com")
    await tx
    assert tx.success
    assert observed == [(True, "system")]
    other.close()
