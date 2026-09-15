# tests/integration/state/test_edge_cases.py
from __future__ import annotations

import pytest

from tests.conftest import StateStack
from tests.schemas.users import UserSchema

USER = {
    "username": "gibril",
    "email": "gibril@example.com",
    "age": 30,
    "active": True,
    "role": "admin",
}


@pytest.mark.asyncio
async def test_duplicate_collection_insert_is_rejected(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    with pytest.raises(ValueError):
        users["user-1"] = USER


@pytest.mark.asyncio
async def test_remove_missing_row_is_rejected(state_stack: StateStack) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    with pytest.raises(KeyError):
        users.remove("missing")


@pytest.mark.asyncio
async def test_empty_strict_set_is_rejected(state_stack: StateStack) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect(state_stack.endpoint)

    inserted = users.insert("user-1", **USER)
    await inserted

    with pytest.raises(ValueError):
        users["user-1"].set()


@pytest.mark.asyncio
async def test_empty_transaction_completes_successfully_without_revision(
    state_stack: StateStack,
) -> None:
    await state_stack.client.connect(state_stack.endpoint)

    revisions = dict(state_stack.store.schema_revisions)
    transaction = state_stack.state.transaction()

    with transaction:
        pass

    await transaction

    assert transaction.success
    assert state_stack.store.schema_revisions == revisions
