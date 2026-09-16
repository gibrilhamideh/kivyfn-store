# tests/integration/state/test_transactions.py
from __future__ import annotations

import pytest

from kivyfn.store.query import Query
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event", ["on_pending", "pending_count", "is_pending"]
)
async def test_close_from_pending_callback_never_sends_transaction(
    state_stack: StateStack, event: str
) -> None:
    stack = state_stack
    users = stack.state.create(UserSchema)
    await stack.client.connect(stack.endpoint)
    await users.insert("user-1", **USER)
    user = users["user-1"]
    before = dict(stack.store.schema_revisions)

    def close_manager(target, value):
        if event == "on_pending" or value:
            stack.state.close()

    user.bind(**{event: close_manager})
    with stack.state.transaction() as tx:
        set_state_value(user, "age", 31)
        users["new-user"] = {**USER, "username": "new-user"}
    await tx

    # This reply follows any work that startup might have wrongly submitted.
    await stack.client.query(Query(UserSchema))

    assert tx.failed
    assert user.pending_count == users.pending_count == 0
    assert not user.is_pending and not users.is_pending
    assert state_value(user, "age") == 30
    assert "new-user" not in users
    canonical = stack.store.snapshot_by_schema("UserSchema")
    assert canonical["user-1"]["age"] == 30
    assert "new-user" not in canonical
    assert stack.store.schema_revisions == before
