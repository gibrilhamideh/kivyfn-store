# tests/integration/state/test_reconnect.py
from __future__ import annotations

import pytest

from tests.conftest import StateStack
from tests.helpers import state_value
from tests.schemas.users import UserSchema

USER = {
    "username": "gibril",
    "email": "gibril@example.com",
    "age": 30,
    "active": True,
    "role": "admin",
}


@pytest.mark.asyncio
async def test_disconnect_and_reconnect_resynchronizes_same_state(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    original = users
    row = users["user-1"]

    await state_stack.client.disconnect()

    assert not users.synced

    await state_stack.client.connect(state_stack.endpoint)

    assert users is original
    assert users.synced
    assert "user-1" in users
    assert users["user-1"] is row
    assert state_value(row, "username") == "gibril"


@pytest.mark.asyncio
async def test_mutation_after_reconnect_still_updates_state(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    await state_stack.client.disconnect()
    await state_stack.client.connect(state_stack.endpoint)

    operation = users["user-1"].set(age=31)
    await operation

    assert getattr(operation, "success")
    assert state_value(users["user-1"], "age") == 31
