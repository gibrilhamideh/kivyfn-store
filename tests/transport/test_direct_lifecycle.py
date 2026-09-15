from __future__ import annotations

from pathlib import Path

import pytest

from kivyfn.store import Store, StoreClient, StoreHost
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
        root, config = (
            create_builder().storage_path(tmp_path / "second").build()
        )
        second_store = Store(root, config)
        second_host = StoreHost(second_store, stack.endpoint)
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
