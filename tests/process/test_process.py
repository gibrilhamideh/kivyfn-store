from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.client.client import StoreClient
from kivyfn.store.builder.config import HostConfig
from kivyfn.store.builder.config import DevToolsConfig
from kivyfn.store.process import ProcessHandle, StoreProcess
from tests.schemas import create_builder


@dataclass
class Handle:
    name: str
    running: bool = False
    ready: bool = False
    ready_payload: dict[str, Any] | None = None
    pid: int | None = None


class Manager:
    def __init__(self) -> None:
        self.handles: dict[str, Handle] = {}
        self.targets: dict[str, Callable[..., None]] = {}
        self.starts: list[tuple[str, tuple[Any, ...], bool, Any, bool]] = []
        self.stops: list[tuple[str, float | None]] = []

    def register(
        self, name: str, target: Callable[..., None]
    ) -> ProcessHandle:
        handle = Handle(name)
        self.handles[name] = handle
        self.targets[name] = target
        return handle

    def start(
        self,
        name: str,
        *,
        args: tuple[Any, ...],
        wait: bool,
        timeout: float | None,
        daemon: bool,
    ) -> None:
        self.starts.append((name, args, wait, timeout, daemon))
        self.handles[name].running = True

    def stop(self, name: str, *, timeout: float | None) -> None:
        self.stops.append((name, timeout))
        self.handles[name].running = False


class Context:
    args: tuple[Any, ...] = ()

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stop: Callable[[], None] = lambda: None
        self.payload: dict[str, Any] | None = None

    def listen(self, callback: Callable[[], None]) -> None:
        self.stop = callback

    def ready(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.started.set()


def test_process_registers_once_and_preserves_manager_controls(
    tmp_path: Path,
) -> None:
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig("http://127.0.0.1:0"))
        .build()
    )
    manager = Manager()
    process = StoreProcess(
        manager,
        config,
        name="  store  ",
        startup_timeout=7,
        shutdown_timeout=4,
    )
    assert not process.registered
    assert process.process is None
    assert process.pid is None
    assert not process.running
    assert not process.ready
    assert process.ready_payload is None
    assert process.endpoint == process.configured_endpoint
    process.stop()
    assert manager.stops == []

    handle = process.start()
    assert process.start() is handle
    assert manager.targets == {"store": StoreProcess.entry}
    assert manager.starts == [("store", (config,), True, 7, False)]
    assert process.registered
    assert process.running

    manager.handles["store"].ready = True
    manager.handles["store"].pid = 123
    manager.handles["store"].ready_payload = {
        "endpoint": "http://127.0.0.1:12345/socket.io"
    }
    assert process.ready
    assert process.pid == 123
    assert process.endpoint == "http://127.0.0.1:12345/socket.io"
    assert process.configured_endpoint == "http://127.0.0.1:0"

    process.stop()
    process.stop()
    assert manager.stops == [("store", 4)]
    assert not process.running
    assert process.start() is handle
    assert len(manager.targets) == 1
    assert len(manager.starts) == 2


@pytest.mark.parametrize("payload", [{}, {"endpoint": ""}, {"endpoint": 7}])
def test_process_ignores_missing_bound_endpoint(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig("http://127.0.0.1:0"))
        .build()
    )
    manager = Manager()
    process = StoreProcess(manager, config)
    process.start()
    manager.handles[process.name].ready_payload = payload
    assert process.client_endpoint is None
    assert process.endpoint == process.configured_endpoint


@pytest.mark.parametrize(
    ("endpoint", "name", "message"),
    [
        ("direct://store", "valid", "cannot connect clients"),
        ("http://127.0.0.1:0", "   ", "cannot be empty"),
    ],
)
def test_process_rejects_unusable_configuration(
    tmp_path: Path, endpoint: str, name: str, message: str
) -> None:
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig(endpoint))
        .build()
    )
    with pytest.raises(ValueError, match=message):
        StoreProcess(Manager(), config, name=name)


def test_process_requires_host_configuration(tmp_path: Path) -> None:
    config = create_builder().storage_path(tmp_path).build()
    manager = Manager()
    with pytest.raises(ValueError, match="requires a HostConfig"):
        StoreProcess(manager, config)
    assert manager.handles == {}


@pytest.mark.asyncio
async def test_child_runner_reports_bound_port_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig("http://127.0.0.1:0"))
        .build()
    )
    context = Context()
    task = asyncio.create_task(StoreProcess._run(context, config))
    client: StoreClient | None = None
    try:
        await asyncio.wait_for(context.started.wait(), 5)
        assert context.payload is not None
        assert context.payload["port"] > 0
        client = StoreClient()
        await client.connect(context.payload["endpoint"])
        assert client.online
    finally:
        if client is not None:
            await client.disconnect()
        context.stop()
        await asyncio.wait_for(task, 5)
    assert task.done()


@pytest.mark.asyncio
async def test_child_runner_cancellation_releases_bound_listener(
    tmp_path: Path,
) -> None:
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig("http://127.0.0.1:0"))
        .build()
    )
    context = Context()
    task = asyncio.create_task(StoreProcess._run(context, config))
    try:
        await asyncio.wait_for(context.started.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert context.payload is not None
    port = context.payload["port"]
    server = await asyncio.start_server(lambda *_: None, "127.0.0.1", port)
    server.close()
    await server.wait_closed()


def test_process_forwards_explicit_devtools_configuration(tmp_path: Path):
    diagnostics = DevToolsConfig(port=8766)
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(HostConfig("http://127.0.0.1:0", devtools=diagnostics))
        .build()
    )
    manager = Manager()
    process = StoreProcess(manager, config)
    process.start()
    assert manager.starts[0][1] == (config,)


@pytest.mark.asyncio
async def test_child_devtools_readiness_and_both_listener_cleanup(
    tmp_path: Path,
):
    from urllib.parse import urlsplit

    from aiohttp import ClientSession

    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(
            HostConfig("http://127.0.0.1:0", devtools=DevToolsConfig(port=0))
        )
        .build()
    )
    context = Context()
    task = asyncio.create_task(StoreProcess._run(context, config))
    try:
        await asyncio.wait_for(context.started.wait(), 5)
        assert context.payload is not None
        url = context.payload["devtools_url"]
        assert isinstance(url, str)
        async with ClientSession() as http:
            async with http.get(url) as response:
                assert response.status == 200
                assert 'id="root"' in await response.text()
    finally:
        context.stop()
        await asyncio.wait_for(task, 5)
    assert context.payload is not None
    for port in (
        context.payload["port"],
        urlsplit(context.payload["devtools_url"]).port,
    ):
        server = await asyncio.start_server(lambda *_: None, "127.0.0.1", port)
        server.close()
        await server.wait_closed()
