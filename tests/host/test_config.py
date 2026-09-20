from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from aiohttp import ClientSession

from kivyfn.store.client.client import StoreClient
from kivyfn.store.host.host import StoreHost
from kivyfn.store.store.store import Store
from kivyfn.store.builder.config import HostConfig
from kivyfn.store.builder.config import DevToolsConfig
from tests.schemas import create_builder


def test_standalone_store_does_not_require_host_config(tmp_path: Path):
    config = create_builder().storage_path(tmp_path).build()
    store = Store(config)
    try:
        store.boot()
        assert store.booted
        with pytest.raises(ValueError, match="requires a HostConfig"):
            StoreHost(store)
    finally:
        store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["direct", "socketio"])
async def test_host_consumes_builder_configuration(
    tmp_path: Path, transport: str
):
    endpoint = (
        f"direct://config-{uuid4().hex}"
        if transport == "direct"
        else "http://127.0.0.1:0"
    )
    hosting = HostConfig(endpoint)
    config = create_builder().storage_path(tmp_path).host(hosting).build()
    store = Store(config)
    host = StoreHost(store)
    client = StoreClient()
    try:
        store.boot()
        await host.start()
        assert host.config is hosting
        assert host.devtools is None
        assert host.devtools_url is None
        bound = host.transport.endpoint.get("endpoint", endpoint)
        await client.connect(bound)
        assert client.online
    finally:
        await client.disconnect()
        await host.stop()
        store.shutdown()


@pytest.mark.asyncio
async def test_explicit_host_config_does_not_change_store_config(
    tmp_path: Path,
):
    config = create_builder().storage_path(tmp_path).build()
    store = Store(config)
    hosting = HostConfig(f"direct://override-{uuid4().hex}")
    host = StoreHost(store, hosting)
    try:
        store.boot()
        await host.start()
        assert host.config is hosting
        assert store.config.host is None
    finally:
        await host.stop()
        store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("devtools", [True, DevToolsConfig()])
async def test_default_devtools_configuration_serves_port_3000(
    tmp_path: Path, devtools: bool | DevToolsConfig
):
    hosting = HostConfig(
        f"direct://default-port-{uuid4().hex}", devtools=devtools
    )
    config = create_builder().storage_path(tmp_path).host(hosting).build()
    store = Store(config)
    host = StoreHost(store)
    try:
        store.boot()
        await host.start()
        assert host.devtools_url == "http://127.0.0.1:18766"
        async with ClientSession() as http:
            async with http.get(host.devtools_url) as response:
                assert response.status == 200
                assert 'id="root"' in await response.text()
    finally:
        await host.stop()
        store.shutdown()


@pytest.mark.asyncio
async def test_occupied_devtools_port_fails_and_releases_host(tmp_path: Path):
    occupied = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = occupied.sockets[0].getsockname()[1]
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(
            HostConfig(
                "http://127.0.0.1:0", devtools=DevToolsConfig(port=port)
            )
        )
        .build()
    )
    store = Store(config)
    host = StoreHost(store)
    replacement = StoreHost(store, HostConfig("http://127.0.0.1:0"))
    try:
        store.boot()
        with pytest.raises(OSError, match="address already in use"):
            await host.start()
        assert not host.running
        assert not host.transport.running
        assert host.devtools is None
        await replacement.start()
        assert replacement.running
    finally:
        await replacement.stop()
        await host.stop()
        store.shutdown()
        occupied.close()
        await occupied.wait_closed()


@pytest.mark.asyncio
async def test_explicit_zero_keeps_ephemeral_devtools_port(tmp_path: Path):
    config = (
        create_builder()
        .storage_path(tmp_path)
        .host(
            HostConfig(
                f"direct://ephemeral-{uuid4().hex}",
                devtools=DevToolsConfig(port=0),
            )
        )
        .build()
    )
    store = Store(config)
    host = StoreHost(store)
    try:
        store.boot()
        await host.start()
        assert host.devtools_url is not None
        port = urlsplit(host.devtools_url).port
        assert port is not None and port > 0
    finally:
        await host.stop()
        store.shutdown()
