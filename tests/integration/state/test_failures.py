# tests/integration/state/test_failures.py
from __future__ import annotations

import asyncio

import pytest

from tests.conftest import StateStack
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
async def test_invalid_field_value_is_rejected_locally(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect()

    with pytest.raises(Exception):
        users["user-1"] = {**USER, "age": 200}

    assert "user-1" not in users


@pytest.mark.asyncio
async def test_strict_duplicate_unique_value_fails_without_membership(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect()

    first = users.insert("user-1", **USER)
    await first

    duplicate = users.insert(
        "user-2",
        **{**USER, "username": "other"},
    )
    await duplicate

    assert getattr(duplicate, "failed")
    assert "user-2" not in users


@pytest.mark.asyncio
async def test_optimistic_duplicate_insert_rolls_back(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    settings = state_stack.state.create(UserSettingsSchema)
    await state_stack.client.connect()

    first = users.insert("user-1", **USER)
    await first

    failed = asyncio.Event()
    users.bind(on_failed=lambda *_: failed.set())

    users["user-2"] = {**USER, "username": "other"}

    assert "user-2" in users

    await failed.wait()

    assert "user-2" not in users
    assert "user-2" not in settings
    assert users.synced
    assert settings.synced


@pytest.mark.asyncio
async def test_failed_transaction_rolls_back_optimistic_membership(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    await state_stack.client.connect()

    first = users.insert("user-1", **USER)
    await first

    transaction = state_stack.state.transaction()

    with transaction:
        users["user-2"] = {**USER, "username": "other"}
        assert "user-2" in users

    await transaction

    assert transaction.failed
    assert not transaction.success
    assert "user-2" not in users
    assert "user-1" in users
