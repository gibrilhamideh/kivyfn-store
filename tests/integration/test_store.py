# tests/integration/test_store.py
from __future__ import annotations

import pytest

from kivyfn.store import Query
from kivyfn.store.protocol.message.requests import InsertRequestAction
from kivyfn.store.protocol.message.responses import (
    QueryResponse,
    TransactionResponse,
)
from tests.conftest import StoreStack
from tests.schemas.settings import UserSettingsSchema
from tests.schemas.users import UserSchema


@pytest.mark.asyncio
async def test_client_queries_store(store_stack: StoreStack) -> None:
    response = await store_stack.client.query(Query(UserSchema))

    assert isinstance(response, QueryResponse)
    assert response.rows == ()


@pytest.mark.asyncio
async def test_client_transaction_creates_user_and_settings(
    store_stack: StoreStack,
) -> None:
    response = await store_stack.client.transaction(
        transaction_id="insert-user",
        actions=(
            InsertRequestAction(
                schema="UserSchema",
                row_id="user-1",
                values=(
                    ("username", "gibril"),
                    ("email", "gibril@example.com"),
                    ("age", 30),
                    ("active", True),
                    ("role", "admin"),
                ),
            ),
        ),
    )

    assert isinstance(response, TransactionResponse)
    assert response.transaction_id == "insert-user"

    users_response = await store_stack.client.query(Query(UserSchema))
    settings_response = await store_stack.client.query(
        Query(UserSettingsSchema)
    )

    assert isinstance(users_response, QueryResponse)
    assert isinstance(settings_response, QueryResponse)
    assert len(users_response.rows) == 1
    assert len(settings_response.rows) == 1

    user = users_response.rows[0]
    settings = settings_response.rows[0]

    assert user.id == "user-1"
    assert user.to_dict() == {
        "username": "gibril",
        "email": "gibril@example.com",
        "age": 30,
        "active": True,
        "role": "admin",
    }
    assert settings.id == "user-1"
    assert settings.to_dict() == {
        "user_id": "user-1",
        "theme": "system",
        "notifications": True,
    }
