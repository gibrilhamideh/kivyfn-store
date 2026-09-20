from __future__ import annotations

from pathlib import Path

import pytest

from kivyfn.store.client.client import StoreClient
from kivyfn.store.host.host import StoreHost
from kivyfn.store.store.store import Store
from kivyfn.store.builder.config import HostConfig
from kivyfn.store.transport.direct.registry import resolve_host
from tests.conftest import running_state_stack
from tests.schemas import create_builder
from tests.schemas.root import RootSchema


@pytest.mark.asyncio
async def test_duplicate_host_start_does_not_remove_running_owner(
    tmp_path: Path,
) -> None:
    async with running_state_stack(
        tmp_path / "first", transport="direct"
    ) as stack:
        config = (
            create_builder()
            .storage_path(tmp_path / "second")
            .host(HostConfig(stack.endpoint))
            .build()
        )
        second_store = Store(config)
        second_host = StoreHost(second_store)
        try:
            second_store.boot()
            with pytest.raises(RuntimeError, match="already registered"):
                await second_host.start()
            assert not second_host.running
            await second_host.stop()
            assert stack.host.running
            name = stack.host.transport.endpoint["name"]
            assert resolve_host(name) is stack.host.transport
            state = stack.state.create(RootSchema)
            await stack.client.connect(stack.endpoint)
            assert state.synced
        finally:
            await second_host.stop()
            second_store.shutdown()


@pytest.mark.asyncio
async def test_direct_host_stop_and_restart_reuses_its_endpoint(
    tmp_path: Path,
) -> None:
    async with running_state_stack(tmp_path, transport="direct") as stack:
        state = stack.state.create(RootSchema)
        await stack.client.connect(stack.endpoint)
        endpoint = stack.client.endpoint
        await stack.host.start()
        await stack.host.stop()
        assert not stack.client.online
        assert not state.synced
        await stack.host.start()
        await stack.client.connect(stack.endpoint)
        assert stack.client.endpoint == endpoint
        assert state.synced


@pytest.mark.asyncio
async def test_direct_host_closes_other_clients_after_callback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_state_stack(tmp_path, transport="direct") as stack:
        await stack.client.connect(stack.endpoint)
        second = StoreClient()
        await second.connect(stack.endpoint)

        def fail_disconnect() -> None:
            raise RuntimeError("observer failed")

        monkeypatch.setattr(stack.client, "on_disconnect", fail_disconnect)
        try:
            await stack.host.stop()
            assert not second.online
            assert not stack.host.running
        finally:
            await second.disconnect()


@pytest.mark.asyncio
async def test_delayed_close_and_message_cannot_damage_replacement_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from kivyfn.store.protocol.message.event import (
        EventMessage,
        SetEventAction,
        SubscriptionEvent,
    )
    from kivyfn.store.transport.direct.client import DirectClientTransport
    from kivyfn.store.transport.direct.host import DirectHostTransport

    async with running_state_stack(tmp_path, transport="direct") as stack:
        state = stack.state.create(RootSchema)
        await stack.client.connect(stack.endpoint)
        transport = stack.client._require_transport()
        assert isinstance(transport, DirectClientTransport)
        host_transport = stack.host.transport
        assert isinstance(host_transport, DirectHostTransport)
        client_id = transport._client_id
        assert client_id is not None
        old_message, old_close = host_transport._clients[client_id]
        subscription = stack.host.subscriptions.for_client(client_id)[0]
        message = EventMessage(
            stack.store.instance_id,
            (
                SubscriptionEvent(
                    subscription.subscription_id,
                    {"RootSchema": 1},
                    (SetEventAction((("title", "obsolete"),)),),
                ),
            ),
        )
        on_message = stack.client.on_message

        def fail_delivery(message: EventMessage) -> None:
            raise RuntimeError("injected Direct delivery failure")

        monkeypatch.setattr(stack.client, "on_message", fail_delivery)
        with pytest.raises(RuntimeError, match="injected"):
            await old_message(message)
        monkeypatch.setattr(stack.client, "on_message", on_message)
        await stack.client.connect()
        assert transport._client_id != client_id
        assert len(host_transport._clients) == len(stack.host.clients) == 1
        assert stack.host.clients.get(client_id) is None
        await old_close()
        await old_message(message)
        assert stack.client.synced and state.synced
        assert transport.connected
        assert len(host_transport._clients) == len(stack.host.clients) == 1
        async with asyncio.timeout(3):
            result = await stack.client.history()
        assert result is not None
