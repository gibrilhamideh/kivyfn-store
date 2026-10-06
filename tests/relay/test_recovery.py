from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kivyfn.store.builder import Branch, Builder, HostConfig, RelayConfig
from kivyfn.store.client.client import StoreClient
from kivyfn.store.error import StoreStateError, StoreTimeoutError
from kivyfn.store.host.host import StoreHost
from kivyfn.store.protocol.message.requests import SingleSubscriptionRequest
from kivyfn.store.state.manager import StateManager
from kivyfn.store.store import Store
from tests.relay.helpers import Gateway, Peer, RelayRoot, Stack, running_stack

pytestmark = pytest.mark.asyncio


async def test_disconnect_after_commit_never_replays_and_resnapshots(
    relay_stack: Stack,
) -> None:
    stack = relay_stack
    peer = await stack.peer(1)
    before = (await peer.response(SingleSubscriptionRequest("RelayRoot")))[
        "responses"
    ][0]
    stack.gateway.gates[(1, "event")] = asyncio.Event()
    tx = peer.transaction(7)
    await peer.submit(tx)
    assert (await peer.next())["type"] == "event"
    await stack.gateway.sio.disconnect(stack.sid)
    sid, registration = await stack.gateway.registration()
    await stack.relay.wait_online(timeout=2)
    assert registration["generation"] != stack.generation
    assert not any(
        f["type"] == "response" and f.get("request_sequence") == 2
        for f in stack.gateway.seen
    )
    assert len(stack.host.clients) == 0
    assert not stack.host.subscriptions._by_id
    assert stack.store.snapshot_by_schema("RelayRoot")["value"] == 7
    new_peer = Peer(stack.gateway, sid, registration["generation"], 1)
    await new_peer.open()
    assert new_peer.prefix != peer.prefix
    stale = await new_peer.send(
        "request", {"requests": []}, generation=stack.generation
    )
    assert stale == {"accepted": False, "code": "stale_generation"}
    new_peer.sequence -= 1  # Rejected stale traffic never advances the stream.
    response = await new_peer.response(
        SingleSubscriptionRequest(
            "RelayRoot", before["store_instance_id"], before["revision"]
        )
    )
    after = response["responses"][0]
    assert after["values"] == {"value": 7}
    assert after["revision"] == before["revision"] + 1
    assert after["subscription_id"] != before["subscription_id"]
    assert after["store_instance_id"] == before["store_instance_id"]
    # No transaction was submitted by the new peer; no hidden replay occurred.
    assert len([f for f in stack.gateway.seen if f["type"] == "event"]) == 1


async def test_exact_cache_resume_and_store_restart_recovery(
    relay_stack: Stack,
) -> None:
    stack = relay_stack
    first = await stack.peer(1)
    original = (await first.response(SingleSubscriptionRequest("RelayRoot")))[
        "responses"
    ][0]
    cached = SingleSubscriptionRequest(
        "RelayRoot", original["store_instance_id"], original["revision"]
    )
    await first.send("close")
    assert (await first.next())["type"] == "closed"
    second = await stack.peer(2)
    response = (await second.response(cached))["responses"][0]
    assert response["values"] is None
    assert response["subscription_id"] != original["subscription_id"]
    await stack.host.stop()
    stack.store.shutdown()
    store = Store(stack.store.config)
    host = StoreHost(store)
    assert host.relay is not None
    store.boot()
    try:
        await host.start()
        sid, registration = await stack.gateway.registration()
        await host.relay.wait_online(timeout=2)
        third = Peer(stack.gateway, sid, registration["generation"], 1)
        await third.open()
        snapshot = (await third.response(cached))["responses"][0]
        assert snapshot["values"] == {"value": 0}
        assert snapshot["store_instance_id"] != original["store_instance_id"]
    finally:
        await host.stop()
        store.shutdown()


async def test_auth_is_refreshed_on_every_connection(
    tmp_path: Path, gateway: Gateway
) -> None:
    count = 0

    async def credentials() -> dict[str, int]:
        nonlocal count
        count += 1
        return {"attempt": count}

    async with running_stack(tmp_path, gateway, auth=credentials) as stack:
        assert count == 1
        await gateway.sio.disconnect(stack.sid)
        await gateway.registration()
        await stack.relay.wait_online(timeout=2)
        assert count == 2
        assert [auth["credentials"] for auth in gateway.credentials] == [
            {"attempt": 1},
            {"attempt": 2},
        ]


async def test_offline_gateway_does_not_block_local_state_or_shutdown(
    tmp_path: Path, gateway: Gateway
) -> None:
    called = asyncio.Event()
    release = asyncio.Event()

    async def credentials() -> dict[str, str]:
        called.set()
        await release.wait()
        return {}

    store = Store(
        Builder()
        .tree(Branch(RelayRoot))
        .storage_path(tmp_path)
        .host(
            HostConfig(
                "direct://offline-relay",
                relay=RelayConfig(
                    gateway.endpoint, "offline-store", credentials
                ),
            )
        )
        .build()
    )
    host = StoreHost(store)
    assert host.relay is not None
    local = StoreClient()
    store.boot()
    try:
        await host.start()
        await called.wait()
        assert host.running
        assert not host.relay.online
        state = StateManager(local).create(RelayRoot)
        await local.connect(host.endpoint)
        transaction = state.set(value=15)
        await transaction
        assert transaction.success and state.value == 15
        with pytest.raises(StoreTimeoutError):
            await host.relay.wait_online(timeout=0.01)
        waiter = asyncio.create_task(host.relay.wait_online())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert host.relay.running
        async with asyncio.timeout(1):
            await host.stop()
        assert not host.relay.running
        with pytest.raises(StoreStateError):
            await host.relay.wait_online()
    finally:
        await local.disconnect()
        await host.stop()
        store.shutdown()


async def test_rejected_authentication_recovers_and_hides_secrets(
    tmp_path: Path, gateway: Gateway
) -> None:
    called = asyncio.Event()
    allow = False

    def credentials() -> dict[str, str]:
        called.set()
        if not allow:
            raise ValueError("SECRET-MUST-NOT-APPEAR")
        return {"token": "renewed"}

    store = Store(
        Builder()
        .tree(Branch(RelayRoot))
        .storage_path(tmp_path)
        .host(
            HostConfig(
                "direct://auth-recovery",
                relay=RelayConfig(
                    gateway.endpoint,
                    "auth-store",
                    credentials,
                    reconnect_interval=0.02,
                    connect_timeout=0.3,
                ),
            )
        )
        .build()
    )
    host = StoreHost(store)
    assert host.relay is not None
    store.boot()
    try:
        await host.start()
        await called.wait()
        assert "SECRET" not in repr(host.relay.last_error)
        assert "authentication" in str(host.relay.last_error)
        gateway.accept = False
        allow = True
        with pytest.raises(StoreTimeoutError):
            await host.relay.wait_online(timeout=0.12)
        assert host.running and not host.relay.online
        gateway.accept = True
        await host.relay.wait_online(timeout=2)
        assert host.relay.last_error is None
    finally:
        await host.stop()
        store.shutdown()


async def test_stop_restart_is_idempotent_and_fences_old_channels(
    relay_stack: Stack,
) -> None:
    stack = relay_stack
    peer = await stack.peer(1)
    await peer.response(SingleSubscriptionRequest("RelayRoot"))
    await asyncio.gather(stack.host.start(), stack.host.start())
    assert len(stack.gateway.credentials) == 1
    await asyncio.gather(stack.host.stop(), stack.host.stop())
    assert not stack.relay.online
    assert not stack.host.clients._clients
    await stack.host.start()
    sid, registration = await stack.gateway.registration()
    await stack.relay.wait_online(timeout=2)
    assert registration["generation"] != stack.generation
    new_peer = Peer(stack.gateway, sid, registration["generation"], 1)
    await new_peer.open()
    await new_peer.response(new_peer.transaction(2))


async def test_registration_mismatch_never_becomes_online(
    tmp_path: Path, gateway: Gateway
) -> None:
    gateway.bad_registration = True
    store = Store(
        Builder()
        .tree(Branch(RelayRoot))
        .storage_path(tmp_path)
        .host(
            HostConfig(
                "direct://bad-register",
                relay=RelayConfig(
                    gateway.endpoint,
                    "store",
                    reconnect_interval=0.02,
                    connect_timeout=0.3,
                ),
            )
        )
        .build()
    )
    host = StoreHost(store)
    assert host.relay is not None
    store.boot()
    try:
        await host.start()
        with pytest.raises(StoreTimeoutError):
            await host.relay.wait_online(timeout=0.15)
        assert host.running
        gateway.bad_registration = False
        await host.relay.wait_online(timeout=2)
    finally:
        await host.stop()
        store.shutdown()
