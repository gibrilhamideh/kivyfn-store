from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from kivyfn.store.client.client import StoreClient
from kivyfn.store.builder.config import HostConfig, StoreConfig
from kivyfn.store.builder.initializer import Initializer
from kivyfn.store.process import StoreProcess
from kivyfn.store.state.manager import StateManager
from tests.schemas import create_builder
from tests.schemas.settings import UserSettingsSchema
from tests.schemas.users import UserSchema


class SeedUser(Initializer):
    def __init__(self, row_id: str) -> None:
        self.row_id = row_id

    def run(self, data: dict[str, Any]) -> dict[str, Any]:
        self._seed_user(data)
        return data

    def _seed_user(self, data: dict[str, Any]) -> None:
        data["UserSchema"] = {
            self.row_id: {
                "username": "initial",
                "email": "initial@example.com",
            }
        }
        data["UserSettingsSchema"] = {self.row_id: {"user_id": self.row_id}}


class PipeContext:
    """A minimal real child supervisor exercising the public adapter hooks."""

    def __init__(self, connection: Connection, config: StoreConfig) -> None:
        self.connection = connection
        self.args = (config,)

    def listen(self, callback: Callable[[], None]) -> None:
        def wait_for_stop() -> None:
            self.connection.recv()
            callback()

        Thread(target=wait_for_stop, daemon=True).start()

    def ready(self, payload: dict[str, Any]) -> None:
        self.connection.send(payload)


def run_child(connection: Connection, config: StoreConfig) -> None:
    try:
        StoreProcess.entry(PipeContext(connection, config))
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_spawned_initializer_state_and_supervised_shutdown(
    tmp_path: Path,
) -> None:
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig("http://127.0.0.1:0"))
        .initializer(SeedUser("initial-user"))
        .build()
    )
    processes = multiprocessing.get_context("spawn")
    parent_connection, child_connection = processes.Pipe()
    process = processes.Process(
        target=run_child, args=(child_connection, config)
    )
    process.start()
    child_connection.close()
    client: StoreClient | None = None
    try:
        ready = await asyncio.to_thread(parent_connection.poll, 5)
        assert ready, "The spawned Store did not become ready"
        payload = parent_connection.recv()
        assert payload["port"] > 0
        client = StoreClient()
        manager = StateManager(client)
        users = manager.create(UserSchema)
        settings = manager.create(UserSettingsSchema)
        await client.connect(payload["endpoint"])
        assert users["initial-user"].username == "initial"
        assert "initial-user" in settings
        assert users["initial-user"].email == "initial@example.com"
        transaction = users.insert(
            "spawned-user", username="spawned", email="spawned@example.com"
        )
        await transaction
        assert transaction.success
        assert "spawned-user" in users
        assert "spawned-user" in settings
        assert users.synced
        assert settings.synced
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            try:
                if process.is_alive():
                    parent_connection.send("stop")
            except (BrokenPipeError, EOFError):
                pass
            finally:
                await asyncio.to_thread(process.join, 5)
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 5)
                parent_connection.close()
                exitcode = process.exitcode
                process.close()
    assert exitcode == 0
