# tests/integration/state/test_bindings.py
from __future__ import annotations

import asyncio

import pytest

from tests.conftest import StateStack
from tests.helpers import set_state_value, state_value, wait_until
from tests.schemas.users import UserSchema

USER = {
    "username": "gibril",
    "email": "gibril@example.com",
    "age": 30,
    "active": True,
    "role": "admin",
}


async def wait_event(event: asyncio.Event) -> None:
    async with asyncio.timeout(1):
        await event.wait()


@pytest.mark.asyncio
async def test_field_binding_fires_once_for_optimistic_confirmation(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    user = users["user-1"]
    ages: list[int] = []

    user.bind(age=lambda _, value: ages.append(int(value)))

    set_state_value(user, "age", 31)

    assert state_value(user, "age") == 31
    assert ages == [31]

    await wait_until(lambda: not bool(getattr(user, "is_pending", False)))

    assert ages == [31]


@pytest.mark.asyncio
async def test_collection_enter_and_insert_binding_order(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    events: list[tuple[str, str]] = []
    inserted = asyncio.Event()

    users.bind(on_enter=lambda _, row_id, __: events.append(("enter", row_id)))

    def on_insert(_: object, row_id: str, __: object) -> None:
        events.append(("insert", row_id))
        inserted.set()

    users.bind(on_insert=on_insert)

    users["user-1"] = USER

    assert events == [("enter", "user-1")]

    await wait_event(inserted)

    assert events == [("enter", "user-1"), ("insert", "user-1")]


@pytest.mark.asyncio
async def test_collection_leave_and_remove_bindings(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    events: list[tuple[str, str]] = []
    removed = asyncio.Event()

    users.bind(on_leave=lambda _, row_id, __: events.append(("leave", row_id)))

    def on_remove(_: object, row_id: str, __: object) -> None:
        events.append(("remove", row_id))
        removed.set()

    users.bind(on_remove=on_remove)

    del users["user-1"]

    assert events == [("leave", "user-1")]

    await wait_event(removed)

    assert events == [("leave", "user-1"), ("remove", "user-1")]


@pytest.mark.asyncio
async def test_operation_lifecycle_bindings(state_stack: StateStack) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    user = users["user-1"]
    events: list[str] = []

    operation = user.set(age=31)

    operation.bind(on_success=lambda *_: events.append("success"))
    operation.bind(on_done=lambda *_: events.append("done"))

    await operation

    assert events == ["success", "done"]
