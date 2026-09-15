# tests/integration/state/test_lifecycle.py
from __future__ import annotations

import pytest

from tests.conftest import StateStack
from tests.schemas.settings import UserSettingsSchema
from tests.schemas.users import UserSchema


@pytest.mark.asyncio
async def test_state_created_before_connect_receives_initial_snapshot(
    state_stack: StateStack,
) -> None:
    users = state_stack.state.create(UserSchema)
    settings = state_stack.state.create(UserSettingsSchema)

    assert not users.synced
    assert not settings.synced

    await state_stack.client.connect(state_stack.endpoint)

    assert users.synced
    assert settings.synced
    assert len(users) == 0
    assert len(settings) == 0


@pytest.mark.asyncio
async def test_state_registry_preserves_collection_identity(
    state_stack: StateStack,
) -> None:
    first = state_stack.state.create(UserSchema)
    second = state_stack.state.create(UserSchema)

    assert first is second

    await state_stack.client.connect(state_stack.endpoint)
    assert first.synced
