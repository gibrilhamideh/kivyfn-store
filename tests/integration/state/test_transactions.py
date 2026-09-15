# tests/integration/state/test_transactions.py
from __future__ import annotations

import pytest

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
async def test_full_state_transaction_creates_triggered_state(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    settings = state_stack.state.create(UserSettingsSchema)
    await state_stack.client.connect(state_stack.endpoint)

    transaction = state_stack.state.transaction()

    with transaction:
        users["user-1"] = USER
        assert "user-1" in users

    assert transaction.closed
    assert not transaction.done

    await transaction

    assert transaction.success
    assert "user-1" in users
    assert "user-1" in settings
    assert state_value(settings["user-1"], "user_id") == "user-1"


@pytest.mark.asyncio
async def test_transaction_coalesces_multiple_sets_to_final_value(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    user = users["user-1"]
    before = state_stack.store.schema_revisions["UserSchema"]

    transaction = state_stack.state.transaction()

    with transaction:
        set_state_value(user, "age", 31)
        set_state_value(user, "age", 32)

    assert state_value(user, "age") == 32

    await transaction

    assert transaction.success
    assert state_value(user, "age") == 32
    assert state_stack.store.schema_revisions["UserSchema"] == before + 1


@pytest.mark.asyncio
async def test_insert_then_remove_in_same_transaction_is_noop(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    before = dict(state_stack.store.schema_revisions)
    transaction = state_stack.state.transaction()

    with transaction:
        users["user-1"] = USER
        del users["user-1"]

    await transaction

    assert transaction.success
    assert "user-1" not in users
    assert state_stack.store.schema_revisions == before


@pytest.mark.asyncio
async def test_transaction_callbacks_fire_after_canonical_state(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    settings = state_stack.state.create(UserSettingsSchema)
    await state_stack.client.connect(state_stack.endpoint)

    observed: list[tuple[str, bool, bool]] = []
    transaction = state_stack.state.transaction()

    transaction.bind(
        on_success=lambda *_: observed.append(
            ("success", "user-1" in users, "user-1" in settings)
        )
    )
    transaction.bind(
        on_done=lambda *_: observed.append(
            ("done", "user-1" in users, "user-1" in settings)
        )
    )

    with transaction:
        users["user-1"] = USER

    await transaction

    assert observed == [("success", True, True), ("done", True, True)]


@pytest.mark.asyncio
async def test_nested_transactions_are_rejected(
    state_stack: StateStack,
) -> None:
    await state_stack.client.connect(state_stack.endpoint)

    outer = state_stack.state.transaction()

    with outer:
        inner = state_stack.state.transaction()

        with pytest.raises(RuntimeError):
            with inner:
                pass

    await outer
    assert outer.success
