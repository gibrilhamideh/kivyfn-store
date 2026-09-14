# tests/integration/state/test_mutations.py
from __future__ import annotations

import asyncio

import pytest

from tests.conftest import StateStack
from tests.helpers import set_state_value, state_value, wait_until
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
async def test_optimistic_insert_is_visible_before_confirmation(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    settings = state_stack.state.create(UserSettingsSchema)
    await state_stack.client.connect()

    inserted = asyncio.Event()
    users.bind(on_insert=lambda *_: inserted.set())

    users["user-1"] = USER

    assert "user-1" in users
    assert state_value(users["user-1"], "username") == "gibril"

    await inserted.wait()

    assert "user-1" in settings
    assert state_value(settings["user-1"], "theme") == "system"


@pytest.mark.asyncio
async def test_strict_insert_is_not_visible_until_confirmed(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect()

    operation = users.insert("user-1", **USER)

    assert "user-1" not in users

    await operation

    assert getattr(operation, "success")
    assert "user-1" in users


@pytest.mark.asyncio
async def test_optimistic_assignment_updates_visible_value_immediately(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect()

    operation = users.insert("user-1", **USER)
    await operation

    user = users["user-1"]
    set_state_value(user, "age", 31)

    assert state_value(user, "age") == 31

    await wait_until(lambda: not bool(getattr(user, "is_pending", False)))

    assert state_value(user, "age") == 31


@pytest.mark.asyncio
async def test_strict_set_keeps_old_value_until_confirmation(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect()

    inserted = users.insert("user-1", **USER)
    await inserted

    user = users["user-1"]
    operation = user.set(age=31)

    assert state_value(user, "age") == 30

    await operation

    assert getattr(operation, "success")
    assert state_value(user, "age") == 31


@pytest.mark.asyncio
async def test_remove_user_cascades_settings(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    settings = state_stack.state.create(UserSettingsSchema)
    await state_stack.client.connect()

    inserted = users.insert("user-1", **USER)
    await inserted

    assert "user-1" in users
    assert "user-1" in settings

    removed = users.remove("user-1")
    await removed

    assert getattr(removed, "success")
    assert "user-1" not in users
    assert "user-1" not in settings


@pytest.mark.asyncio
async def test_same_value_assignment_is_a_noop(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect()

    inserted = users.insert("user-1", **USER)
    await inserted

    user = users["user-1"]
    revisions = dict(state_stack.store.schema_revisions)

    set_state_value(user, "age", 30)
    await asyncio.sleep(0)

    assert state_value(user, "age") == 30
    assert state_stack.store.schema_revisions == revisions
