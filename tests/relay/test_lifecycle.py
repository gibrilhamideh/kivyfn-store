from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
from typing import Any

import pytest
import socketio

from kivyfn.store.builder import Branch, Builder, HostConfig, RelayConfig
from tests.process.test_spawn import run_child
from tests.relay.helpers import (
    Gateway,
    Peer,
    RelayRoot,
    Stack,
    process_auth,
    running_stack,
)

pytestmark = pytest.mark.asyncio


async def test_store_process_starts_relay_and_stops_cleanly(
    tmp_path: Path, gateway: Gateway
) -> None:
    config = (
        Builder()
        .tree(Branch(RelayRoot))
        .storage_path(tmp_path)
        .host(
            HostConfig(
                "http://127.0.0.1:0",
                relay=RelayConfig(
                    gateway.endpoint, "spawned-app", process_auth
                ),
            )
        )
        .build()
    )
    processes = multiprocessing.get_context("spawn")
    parent, child = processes.Pipe()
    process = processes.Process(target=run_child, args=(child, config))
    process.start()
    child.close()
    try:
        assert await asyncio.to_thread(parent.poll, 5)
        endpoint = parent.recv()
        assert endpoint["port"] > 0
        sid, registration = await gateway.registration()
        assert registration["store_id"] == "spawned-app"
        peer = Peer(gateway, sid, registration["generation"], 1)
        await peer.open()
        response = await peer.response(peer.transaction(6))
        assert response["responses"][0]["type"] == "transaction"
    finally:
        if process.is_alive():
            parent.send("stop")
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        parent.close()
        exitcode = process.exitcode
        process.close()
    assert exitcode == 0


async def test_polling_fallback_and_prompt_shutdown(
    tmp_path: Path, gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = socketio.AsyncClient.connect

    async def polling(self: socketio.AsyncClient, *args: Any, **kwargs: Any):
        kwargs["transports"] = ["polling"]
        return await connect(self, *args, **kwargs)

    monkeypatch.setattr(socketio.AsyncClient, "connect", polling)
    async with running_stack(tmp_path, gateway, connect_timeout=10) as stack:
        peer = await stack.peer(1)
        await peer.response(peer.transaction(6))
        session = stack.relay._session
        assert session is not None
        assert session.sio.transport() == "polling"
        async with asyncio.timeout(1):
            await stack.host.stop()


async def test_cancelled_stop_waiter_does_not_cancel_cleanup(
    relay_stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = relay_stack.relay
    session = relay._session
    assert session is not None
    entered, release = asyncio.Event(), asyncio.Event()
    close = session.close

    async def gated_close() -> None:
        entered.set()
        await release.wait()
        await close()

    monkeypatch.setattr(session, "close", gated_close)
    first = asyncio.create_task(relay.stop())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(relay.stop())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await second
    assert not relay.running
    assert not relay.online


async def test_missing_close_receipt_fences_the_entire_connection(
    tmp_path: Path, gateway: Gateway
) -> None:
    async with running_stack(tmp_path, gateway, request_timeout=0.04) as stack:
        peer = await stack.peer(1)
        gateway.gates[(1, "closed")] = asyncio.Event()
        await peer.send("close")
        assert (await peer.next())["type"] == "closed"
        _, registration = await gateway.registration()
        await stack.relay.wait_online(timeout=2)
        assert registration["generation"] != stack.generation


async def test_registration_handoff_buffers_open_and_requests(
    tmp_path: Path, gateway: Gateway
) -> None:
    # A real server can send open immediately after queueing its register ACK.
    register = gateway.register

    async def early_open(sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        reply = await register(sid, payload)
        await gateway.sio.emit(
            "store_relay",
            {
                "generation": payload["generation"],
                "channel_id": 1,
                "sequence": 0,
                "type": "open",
                "payload": {"protocol_version": 3},
            },
            to=sid,
        )
        return reply

    gateway.sio.on("store_register", early_open)
    async with running_stack(tmp_path, gateway) as stack:
        frame = await gateway.next_frame(stack.sid, 1)
        assert frame["type"] == "opened"
        assert len(stack.host.clients) == 1
