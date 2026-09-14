# tests/integration/state/test_queries.py
from __future__ import annotations

import asyncio

import pytest

from kivyfn.store import Query, field

from tests.conftest import StateStack
from tests.helpers import set_state_value, state_value
from tests.schemas.settings import UserSettingsSchema
from tests.schemas.users import UserSchema


USER = {
    "username": "gibril",
    "email": "gibril@example.com",
    "age": 30,
    "active": True,
    "role": "admin",
}


@pytest.mark.asyncio
async def test_query_subscription_initial_snapshot(
    state_stack: StateStack,
) -> None:
    enabled = True
    active_users = state_stack.state.create(
        Query(UserSchema).where(field("active") == enabled)
    )

    await state_stack.client.connect()

    assert active_users.synced
    assert len(active_users) == 0


@pytest.mark.asyncio
async def test_query_membership_enters_and_leaves(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    enabled = True
    active_users = state_stack.state.create(
        Query(UserSchema).where(field("active") == enabled)
    )

    entered: list[str] = []
    left: list[str] = []

    active_users.bind(
        on_enter=lambda _, row_id, __: entered.append(row_id)
    )
    active_users.bind(
        on_leave=lambda _, row_id, __: left.append(row_id)
    )

    await state_stack.client.connect()

    inserted = users.insert("user-1", **{**USER, "active": False})
    await inserted

    assert "user-1" not in active_users

    user = users["user-1"]
    activated = user.set(active=True)
    await activated

    assert "user-1" in active_users
    assert entered == ["user-1"]

    deactivated = user.set(active=False)
    await deactivated

    assert "user-1" not in active_users
    assert left == ["user-1"]


@pytest.mark.asyncio
async def test_query_stays_synced_when_dependency_changes_without_visible_row(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    enabled = True
    active_users = state_stack.state.create(
        Query(UserSchema).where(field("active") == enabled)
    )

    await state_stack.client.connect()

    inserted = users.insert("user-1", **{**USER, "active": False})
    await inserted

    assert len(active_users) == 0

    user = users["user-1"]
    renamed = user.set(username="new-name")
    await renamed

    assert len(active_users) == 0
    assert active_users.synced


@pytest.mark.asyncio
async def test_query_join_exposes_user_fields(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    joined = state_stack.state.create(
        Query(UserSettingsSchema).join("user_id", as_="user")
    )

    await state_stack.client.connect()

    inserted = users.insert("user-1", **USER)
    await inserted

    assert "user-1" in joined

    row = joined["user-1"]

    assert state_value(row, "user_id") == "user-1"
    assert state_value(row, "user_username") == "gibril"
    assert state_value(row, "user_email") == "gibril@example.com"


@pytest.mark.asyncio
async def test_joined_field_is_writable_through_query_state(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    joined = state_stack.state.create(
        Query(UserSettingsSchema).join("user_id", as_="user")
    )

    await state_stack.client.connect()

    inserted = users.insert("user-1", **USER)
    await inserted

    row = joined["user-1"]
    set_state_value(row, "user_username", "updated")

    user = users["user-1"]

    async with asyncio.timeout(1.0):
        while state_value(user, "username") != "updated":
            await asyncio.sleep(0)

    assert state_value(row, "user_username") == "updated"
    assert state_value(user, "username") == "updated"
