# tests/conftest.py
from __future__ import annotations

import shutil
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from kivyfn.logging import get_logger
from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.state.manager import StateManager

from tests.schemas import TEST_STORE_PATH, create_builder


logger = get_logger(__name__)


@dataclass(slots=True)
class StoreStack:
    path: Path
    store: Store
    host: StoreHost
    client: StoreClient


@dataclass(slots=True)
class StateStack(StoreStack):
    state: StateManager


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _test_path(name: str) -> Path:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in name
    )
    return TEST_STORE_PATH / safe


@asynccontextmanager
async def running_store_stack(
    path: Path,
    *,
    clean: bool = True,
) -> AsyncIterator[StoreStack]:
    if clean:
        shutil.rmtree(path, ignore_errors=True)

    root, config = create_builder().storage_path(path).build()

    store = Store(root=root, config=config)
    store.boot()

    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"

    host = StoreHost(store, endpoint)
    client = StoreClient(endpoint)

    logger.info("Starting Store stack endpoint=%s path=%s", endpoint, path)

    await host.start()
    await client.connect()

    try:
        yield StoreStack(path=path, store=store, host=host, client=client)
    finally:
        await client.disconnect()
        await host.stop()
        store.shutdown()


@asynccontextmanager
async def running_state_stack(
    path: Path,
    *,
    clean: bool = True,
) -> AsyncIterator[StateStack]:
    if clean:
        shutil.rmtree(path, ignore_errors=True)

    root, config = create_builder().storage_path(path).build()

    store = Store(root=root, config=config)
    store.boot()

    port = _free_port()
    endpoint = f"http://127.0.0.1:{port}"

    host = StoreHost(store, endpoint)
    client = StoreClient(endpoint)
    state = StateManager(client)

    logger.info("Starting State stack endpoint=%s path=%s", endpoint, path)

    await host.start()

    try:
        yield StateStack(
            path=path,
            store=store,
            host=host,
            client=client,
            state=state,
        )
    finally:
        await client.disconnect()
        await host.stop()
        store.shutdown()


@pytest.fixture
def store_path(request: pytest.FixtureRequest) -> Path:
    return _test_path(request.node.name)


@pytest_asyncio.fixture
async def store_stack(store_path: Path) -> AsyncIterator[StoreStack]:
    async with running_store_stack(store_path) as stack:
        yield stack


@pytest_asyncio.fixture
async def state_stack(store_path: Path) -> AsyncIterator[StateStack]:
    async with running_state_stack(store_path) as stack:
        yield stack
