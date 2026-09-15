# tests/conftest.py
from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from logging import getLogger as get_logger
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
import pytest_asyncio

from kivyfn.store import HostConfig, Store, StoreClient, StoreHost
from kivyfn.store.state.manager import StateManager
from tests.schemas import create_builder

logger = get_logger(__name__)

TransportKind = Literal["direct", "socketio"]


@dataclass(slots=True)
class StoreStack:
    path: Path
    store: Store
    host: StoreHost
    client: StoreClient
    endpoint: str


@dataclass(slots=True)
class StateStack(StoreStack):
    state: StateManager


def _host_endpoint(transport: TransportKind) -> str:
    if transport == "direct":
        return f"direct://test-{uuid4().hex}"
    return "http://127.0.0.1:0"


def _client_endpoint(host: StoreHost) -> str:
    endpoint = host.transport.endpoint
    if endpoint["transport"] == "direct":
        return host.endpoint
    return str(endpoint["endpoint"])


@asynccontextmanager
async def running_store_stack(
    path: Path, *, clean: bool = True, transport: TransportKind = "socketio"
) -> AsyncIterator[StoreStack]:
    async with running_state_stack(
        path, clean=clean, transport=transport
    ) as stack:
        await stack.client.connect(stack.endpoint)
        yield stack


@asynccontextmanager
async def running_state_stack(
    path: Path, *, clean: bool = True, transport: TransportKind = "socketio"
) -> AsyncIterator[StateStack]:
    if clean:
        shutil.rmtree(path, ignore_errors=True)

    config = create_builder().storage_path(path).build()

    store = Store(config)
    endpoint = _host_endpoint(transport)
    host = StoreHost(store, HostConfig(endpoint))
    client: StoreClient | None = None

    try:
        store.boot()
        await host.start()
        client = StoreClient()
        state = StateManager(client)
        endpoint = _client_endpoint(host)
        logger.info("State stack endpoint=%s path=%s", endpoint, path)
        yield StateStack(
            path=path,
            store=store,
            host=host,
            client=client,
            endpoint=endpoint,
            state=state,
        )
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            try:
                await host.stop()
            finally:
                store.shutdown()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture(params=["direct", "socketio"])
def transport_kind(request: pytest.FixtureRequest) -> TransportKind:
    return request.param


@pytest_asyncio.fixture
async def store_stack(
    store_path: Path, transport_kind: TransportKind
) -> AsyncIterator[StoreStack]:
    async with running_store_stack(
        store_path, transport=transport_kind
    ) as stack:
        yield stack


@pytest_asyncio.fixture
async def state_stack(
    store_path: Path, transport_kind: TransportKind
) -> AsyncIterator[StateStack]:
    async with running_state_stack(
        store_path, transport=transport_kind
    ) as stack:
        yield stack
