# tests/integration/state/test_persistence.py
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import TransportKind, running_state_stack
from tests.helpers import state_value
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
async def test_persistent_state_survives_store_restart(
    store_path: Path, transport_kind: TransportKind
) -> None:
    async with running_state_stack(
        store_path, transport=transport_kind
    ) as first:
        users = first.state.create(UserSchema)
        settings = first.state.create(UserSettingsSchema)
        await first.client.connect(first.endpoint)

        inserted = users.insert("user-1", **USER)
        await inserted

        assert "user-1" in users
        assert "user-1" in settings

    async with running_state_stack(
        store_path, clean=False, transport=transport_kind
    ) as second:
        users = second.state.create(UserSchema)
        settings = second.state.create(UserSettingsSchema)
        await second.client.connect(second.endpoint)

        assert "user-1" in users
        assert "user-1" in settings
        assert state_value(users["user-1"], "username") == "gibril"
        assert state_value(settings["user-1"], "theme") == "system"


@pytest.mark.asyncio
async def test_persistent_delete_survives_restart(
    store_path: Path, transport_kind: TransportKind
) -> None:
    async with running_state_stack(
        store_path, transport=transport_kind
    ) as first:
        users = first.state.create(UserSchema)
        await first.client.connect(first.endpoint)

        inserted = users.insert("user-1", **USER)
        await inserted

        removed = users.remove("user-1")
        await removed

        assert "user-1" not in users

    async with running_state_stack(
        store_path, clean=False, transport=transport_kind
    ) as second:
        users = second.state.create(UserSchema)
        settings = second.state.create(UserSettingsSchema)
        await second.client.connect(second.endpoint)

        assert "user-1" not in users
        assert "user-1" not in settings
