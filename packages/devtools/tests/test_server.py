# tests/devtools/test_server.py
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.builder.config import HistoryConfig, HostConfig
from kivyfn.store.devtools import DevToolsConfig
from kivyfn.store.devtools.devtools import DevTools
from kivyfn.store.devtools.protocol import from_wire
from kivyfn.store.devtools.server import DevToolsServer
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    SetRequestAction,
    SetRowRequestAction,
    SignalRequestAction,
)
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.fields import DataField, NumericField, StringField
from kivyfn.store.schema.signal import Signal
from tests.conftest import TransportKind


class BrowserModel(SingleSchema):
    track_history = True
    x = NumericField(default=0)
    title = StringField(default="hello")
    open_window = Signal[dict[str, str]](undo="close_window")
    close_window = Signal[dict[str, str]](undo="open_window")


class BrowserRows(ManySchema):
    track_history = True
    x = NumericField(default=0)
    payload = DataField[Any](default=None)


@dataclass
class BrowserStack:
    store: Store
    host: StoreHost
    client: StoreClient
    http: ClientSession
    url: str


@pytest_asyncio.fixture
async def browser_stack(
    tmp_path: Path, transport_kind: TransportKind
) -> AsyncIterator[BrowserStack]:
    endpoint = (
        f"direct://browser-{uuid4().hex}"
        if transport_kind == "direct"
        else "http://127.0.0.1:0"
    )
    config = (
        Builder()
        .tree(Branch(BrowserModel).child("rows", Branch(BrowserRows)))
        .storage_path(tmp_path)
        .history(HistoryConfig())
        .host(HostConfig(endpoint, devtools=DevToolsConfig(port=0)))
        .build()
    )
    store = Store(config)
    host = StoreHost(store)
    client = StoreClient()
    store.boot()
    try:
        await host.start()
        address = host.transport.endpoint
        await client.connect(
            endpoint if transport_kind == "direct" else address["endpoint"]
        )
        assert host.devtools_url is not None
        async with ClientSession() as http:
            yield BrowserStack(store, host, client, http, host.devtools_url)
    finally:
        await client.disconnect()
        await host.stop()
        store.shutdown()


async def receive(
    websocket: ClientWebSocketResponse, kind: str
) -> dict[str, Any]:
    async with asyncio.timeout(5):
        for _ in range(100):
            message = await websocket.receive()
            assert message.type == WSMsgType.TEXT, message
            envelope = message.json()
            if envelope["kind"] == kind:
                return envelope
    raise AssertionError(f"Missing {kind} envelope")


async def connect(
    stack: BrowserStack,
) -> tuple[ClientWebSocketResponse, dict[str, Any], dict[str, str]]:
    websocket = await stack.http.ws_connect(stack.url + "/stream")
    initial = await receive(websocket, "snapshot")
    identity = initial["payload"]["session_id"]
    return websocket, initial, {"X-DevTools-Session": identity}


def command(value: Any, *, identity: str | None = None) -> dict[str, Any]:
    return {
        "requests": [
            {
                "type": "transaction",
                "transaction_id": identity or uuid4().hex,
                "actions": [
                    {
                        "type": "set",
                        "schema": "BrowserModel",
                        "values": {"x": value},
                    }
                ],
            }
        ]
    }


@pytest.mark.asyncio
async def test_real_assets_sessions_commands_history_and_preview(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    response = await stack.http.get(stack.url)
    assert response.status == 200
    assert '<div id="root">' in await response.text()
    websocket, initial, headers = await connect(stack)
    other, _, other_headers = await connect(stack)
    assert headers != other_headers
    assert (
        initial["payload"]["checkpoint"]["schemas"]["BrowserModel"]["x"] == 0
    )
    request = command(17)
    result = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json=request
    )
    wire = await result.json()
    assert wire["responses"][0]["type"] == "transaction"
    committed = await receive(websocket, "commit")
    payload = committed["payload"]
    assert payload["events"][0]["new"] == 17
    assert payload["origin_client_id"] == headers["X-DevTools-Session"]
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 17
    history = await stack.http.get(stack.url + "/api/history", headers=headers)
    page = await history.json()
    assert len(page["entries"]) == 1
    undo = {
        "requests": [
            {
                "type": "undo",
                "transaction_id": "browser-undo",
                "expected_position": page["status"]["undo_position"],
            }
        ]
    }
    for _ in range(2):
        result = await stack.http.post(
            stack.url + "/api/commands", headers=headers, json=undo
        )
        assert (await result.json())["responses"][0]["type"] == "transaction"
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 0
    assert stack.store.history().status.can_redo
    preview = await stack.http.get(
        stack.url + f"/api/preview?sequence={payload['sequence']}",
        headers=headers,
    )
    past = await preview.json()
    assert past["available"] is True
    assert past["checkpoint"]["schemas"]["BrowserModel"]["x"] == 17
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 0
    await websocket.close()
    await other.close()


@pytest.mark.asyncio
async def test_application_client_and_browser_have_independent_ownership(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    await stack.client.transaction(
        transaction_id="application-write",
        actions=(SetRequestAction("BrowserModel", (("x", 21),)),),
    )
    envelope = await receive(websocket, "commit")
    assert envelope["payload"]["transaction_id"] == "application-write"
    assert (
        envelope["payload"]["origin_client_id"]
        != headers["X-DevTools-Session"]
    )
    await websocket.close()
    await stack.client.transaction(
        transaction_id="after-browser-close",
        actions=(SetRequestAction("BrowserModel", (("x", 22),)),),
    )
    assert stack.client.online
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 22


@pytest.mark.asyncio
async def test_browser_boundaries_and_stale_guards(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, initial, headers = await connect(stack)
    for extra in ({"Origin": "https://evil.example"}, {"Host": "evil.test"}):
        response = await stack.http.post(
            stack.url + "/api/commands",
            headers={**headers, **extra},
            json=command(1),
        )
        assert response.status == 403
    response = await stack.http.post(
        stack.url + "/api/commands",
        json=command(1),
        headers={"X-DevTools-Session": "spoofed"},
    )
    assert response.status == 401
    response = await stack.http.post(
        stack.url + "/api/commands",
        headers=headers,
        json={
            "requests": [
                {"type": "subscribe_single", "schema": "BrowserModel"}
            ]
        },
    )
    assert response.status == 400
    await stack.client.transaction(
        transaction_id="intervening",
        actions=(SetRequestAction("BrowserModel", (("x", 10),)),),
    )
    stale = command(20)
    stale["requests"][0].update(
        {
            "expected_store_instance_id": initial["store_instance_id"],
            "expected_revisions": {"BrowserModel": 0},
        }
    )
    response = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json=stale
    )
    assert (await response.json())["responses"][0][
        "type"
    ] == "transaction_failure"
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 10
    assert stack.client.online
    await websocket.close()


@pytest.mark.asyncio
async def test_large_integers_scope_epochs_and_paused_live_feed(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, initial, headers = await connect(stack)
    response = await stack.http.post(
        stack.url + "/api/commands",
        headers=headers,
        json=command({"$integer": str(2**80)}),
    )
    assert (await response.json())["responses"][0]["type"] == "transaction"
    envelope = await receive(websocket, "commit")
    assert from_wire(envelope["payload"])["events"][0]["new"] == 2**80
    await websocket.send_json({"kind": "scope", "schemas": ["BrowserModel"]})
    scoped = await receive(websocket, "snapshot")
    assert scoped["epoch"] != initial["epoch"]
    assert scoped["sequence"] == 1
    response = await stack.http.post(
        stack.url + "/api/capture", headers=headers, json={"paused": True}
    )
    assert response.status == 200
    await stack.client.transaction(
        transaction_id="while-paused",
        actions=(SetRequestAction("BrowserModel", (("x", 3),)),),
    )
    live = await receive(websocket, "commit")
    assert live["payload"]["events"][0]["new"] == 3
    assert stack.store.history().status.can_undo
    preview = await stack.http.get(
        stack.url + f"/api/preview?sequence={live['payload']['sequence']}",
        headers=headers,
    )
    assert (await preview.json())["available"] is False
    await websocket.close()


@pytest.mark.asyncio
async def test_recorder_failure_and_slow_writer_leave_store_running(
    browser_stack: BrowserStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    facade = stack.host.devtools
    assert facade is not None
    server = facade.server
    session = server.sessions[headers["X-DevTools-Session"]]
    server.config = replace(server.config, max_queue_messages=8)
    entered, release = asyncio.Event(), asyncio.Event()
    original = session.websocket.send_str

    async def blocked_send(data: str, compress: int | None = None) -> None:
        entered.set()
        await release.wait()
        await original(data, compress=compress)

    monkeypatch.setattr(session.websocket, "send_str", blocked_send)
    stack.store.execute(
        (SetRequestAction("BrowserModel", (("x", 1),)),),
        transaction_id="first",
    )
    await entered.wait()
    for value in range(2, 25):
        stack.store.execute(
            (SetRequestAction("BrowserModel", (("x", value),)),),
            transaction_id=f"burst-{value}",
        )
    assert session.resync
    assert len(session.queue) <= 8
    assert session.queue_bytes <= server.config.max_queue_bytes
    release.set()
    await receive(websocket, "gap")
    resynced = await receive(websocket, "snapshot")
    assert (
        resynced["payload"]["checkpoint"]["schemas"]["BrowserModel"]["x"] == 24
    )

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Injected diagnostic failure")

    monkeypatch.setattr(facade.recorder, "commit", broken)
    await stack.client.transaction(
        transaction_id="capture-failed",
        actions=(SetRequestAction("BrowserModel", (("x", 25),)),),
    )
    assert stack.host.running
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 25
    await websocket.close()


@pytest.mark.asyncio
async def test_cancelled_stop_waiter_does_not_abandon_cleanup(
    browser_stack: BrowserStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = browser_stack
    websocket, _, _ = await connect(stack)
    facade = stack.host.devtools
    assert facade is not None
    server = facade.server
    entered, release = asyncio.Event(), asyncio.Event()
    original = server._close_session

    async def blocked_close(session: Any) -> None:
        entered.set()
        await release.wait()
        await original(session)

    monkeypatch.setattr(server, "_close_session", blocked_close)
    waiter = asyncio.create_task(server.stop())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await server.stop()
    assert not server.sessions
    assert server.runner is None
    assert server.url is None
    await websocket.close()


@pytest.mark.asyncio
async def test_missing_assets_unwinds_listener_and_observers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = (
        Builder()
        .tree(Branch(BrowserModel))
        .storage_path(tmp_path)
        .host(
            HostConfig(
                f"direct://missing-{uuid4().hex}",
                devtools=DevToolsConfig(port=0),
            )
        )
        .build()
    )
    store = Store(config)
    store.boot()
    host = StoreHost(store)
    original = DevToolsServer.__init__

    def missing(self: DevToolsServer, facade: DevTools) -> None:
        original(self, facade)
        self.static = tmp_path / "does-not-exist"

    monkeypatch.setattr(DevToolsServer, "__init__", missing)
    with pytest.raises(RuntimeError, match="assets are missing"):
        await host.start()
    assert not host.running
    assert host.devtools_url is None
    assert not store._commit_listeners
    assert not store._history_listeners
    await host.stop()
    store.shutdown()


class UntrackedHugeModel(SingleSchema):
    payload = StringField(default="")


@pytest.mark.asyncio
async def test_initial_scope_avoids_oversized_unselected_schema(
    tmp_path: Path,
) -> None:
    config = (
        Builder()
        .tree(Branch(BrowserModel).child("huge", Branch(UntrackedHugeModel)))
        .storage_path(tmp_path)
        .history(HistoryConfig())
        .host(
            HostConfig(
                f"direct://scoped-{uuid4().hex}",
                devtools=DevToolsConfig(
                    port=0,
                    max_bytes=32_768,
                    max_payload_bytes=8192,
                    max_queue_bytes=32_768,
                ),
            )
        )
        .build()
    )
    store = Store(config)
    store.boot()
    store.execute(
        (
            SetRequestAction(
                "UntrackedHugeModel", (("payload", "x" * 40_000),)
            ),
        ),
        transaction_id="large-untracked",
    )
    host = StoreHost(store)
    try:
        await host.start()
        assert host.devtools_url is not None
        async with ClientSession() as http:
            websocket = await http.ws_connect(
                host.devtools_url + "/stream?schemas=BrowserModel"
            )
            initial = await receive(websocket, "snapshot")
            checkpoint = initial["payload"]["checkpoint"]
            assert list(checkpoint["schemas"]) == ["BrowserModel"]
            identity = initial["payload"]["session_id"]
            headers = {"X-DevTools-Session": identity}
            facade = host.devtools
            assert facade is not None
            sequence = facade.recorder.sequence
            preview = await http.get(
                host.devtools_url + f"/api/preview?sequence={sequence}",
                headers=headers,
            )
            result = await preview.json()
            assert result["available"]
            assert result["scope"] == ["BrowserModel"]
            assert "UntrackedHugeModel" not in result["checkpoint"]["schemas"]
            await websocket.close()
    finally:
        await host.stop()
        store.shutdown()


@pytest.mark.asyncio
async def test_failures_read_capture_and_limits_at_http_boundary(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    facade = stack.host.devtools
    assert facade is not None
    response = await stack.http.post(
        stack.url + "/api/capture",
        headers=headers,
        json={"filters": {"status": "failed", "schema": "BrowserModel"}},
    )
    assert response.status == 200
    failed = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json=command("wrong")
    )
    assert (await failed.json())["responses"][0][
        "type"
    ] == "transaction_failure"
    records = facade.recorder.activity()["records"]
    failures = [record for record in records if record["status"] == "failed"]
    assert len(failures) == 1
    assert failures[0]["category"] == "response"
    assert failures[0]["schemas"] == ["BrowserModel"]
    assert not facade.recorder.pending
    await stack.http.post(
        stack.url + "/api/capture", headers=headers, json={"filters": {}}
    )
    query = {
        "requests": [
            {
                "type": "query",
                "query": {
                    "version": 1,
                    "source": {"schema": "BrowserRows"},
                    "joins": [],
                    "predicate": None,
                },
            }
        ]
    }
    response = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json=query
    )
    assert (await response.json())["responses"][0]["type"] == "query"
    request_records = [
        record
        for record in facade.recorder.activity()["records"]
        if record["category"] == "request"
    ]
    assert request_records[-1]["detail"]["request"] == query
    response = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json={"invalid": True}
    )
    assert response.status == 400
    assert facade.recorder.activity()["records"][-1]["category"] == "boundary"
    response = await stack.http.post(
        stack.url + "/api/commands",
        headers=headers,
        json=command("x" * (facade.config.max_request_bytes + 1)),
    )
    assert response.status == 413
    assert stack.host.running
    await websocket.close()


@pytest.mark.asyncio
async def test_pending_correlations_are_bounded_and_cancelled_are_finalized(
    browser_stack: BrowserStack,
) -> None:
    from kivyfn.store.protocol.message.requests import (
        RequestBatch,
        TransactionRequest,
    )

    stack = browser_stack
    facade = stack.host.devtools
    assert facade is not None
    facade.config = replace(facade.config, max_pending_captures=2)
    batch = RequestBatch(
        (
            TransactionRequest(
                "correlation", (SetRequestAction("BrowserModel", (("x", 1),)),)
            ),
        )
    )
    first = facade.request("client", batch)
    second = facade.request("client", batch)
    third = facade.request("client", batch)
    assert first is not None and second is not None and third is None
    assert len(facade.recorder.pending) == 2
    requests = [
        record
        for record in facade.recorder.activity()["records"]
        if record["category"] == "request"
    ]
    assert len(requests) == 2
    assert all(record["status"] == "received" for record in requests)
    facade.failure(first, asyncio.CancelledError(), cancelled=True)
    facade.failure(second, RuntimeError("injected"))
    assert not facade.recorder.pending
    statuses = [
        record["status"] for record in facade.recorder.activity()["records"]
    ]
    assert "cancelled" in statuses
    assert "failed" in statuses
    assert statuses.count("received") == 2


@pytest.mark.asyncio
async def test_pending_http_commands_have_bounded_admission(
    browser_stack: BrowserStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp import web

    from kivyfn.store.protocol.message.requests import RequestBatch

    stack = browser_stack
    websocket, _, headers = await connect(stack)
    facade = stack.host.devtools
    assert facade is not None
    server = facade.server
    server.config = replace(server.config, max_pending_commands=2)
    session = server.sessions[headers["X-DevTools-Session"]]
    entered, queued, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    request_original = stack.host.request
    json_original = web.Request.json

    async def blocked(identity: str, batch: RequestBatch) -> Any:
        if getattr(batch.items[0], "transaction_id", None) == "admitted-first":
            entered.set()
            await release.wait()
        return await request_original(identity, batch)

    async def parsed(request: web.Request, **kwargs: Any) -> Any:
        payload = await json_original(request, **kwargs)
        if payload["requests"][0]["transaction_id"] == "admitted-second":
            queued.set()
        return payload

    async def post(value: int, identity: str) -> Any:
        return await stack.http.post(
            stack.url + "/api/commands",
            headers=headers,
            json=command(value, identity=identity),
        )

    monkeypatch.setattr(stack.host, "request", blocked)
    monkeypatch.setattr(web.Request, "json", parsed)
    first = asyncio.create_task(post(1, "admitted-first"))
    await entered.wait()
    second = asyncio.create_task(post(2, "admitted-second"))
    await queued.wait()
    assert session.pending_commands == 2
    excess = await post(3, "rejected-third")
    assert excess.status == 429
    assert session.pending_commands == 2
    release.set()
    for response in await asyncio.gather(first, second):
        assert (await response.json())["responses"][0]["type"] == "transaction"
    assert session.pending_commands == 0
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 2
    await websocket.close()


@pytest.mark.asyncio
async def test_empty_initial_scope_is_a_valid_session(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket = await stack.http.ws_connect(stack.url + "/stream?schemas=")
    snapshot = await receive(websocket, "snapshot")
    assert snapshot["payload"]["checkpoint"]["schemas"] == {}
    assert snapshot["payload"]["session_id"]
    await websocket.close()


@pytest.mark.asyncio
async def test_closed_session_token_cannot_submit_commands(
    browser_stack: BrowserStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    identity = headers["X-DevTools-Session"]
    disconnected = asyncio.Event()
    original = stack.host.disconnect

    async def observe(client_id: str) -> None:
        await original(client_id)
        if client_id == identity:
            disconnected.set()

    monkeypatch.setattr(stack.host, "disconnect", observe)
    await websocket.close()
    async with asyncio.timeout(5):
        await disconnected.wait()
    assert stack.host.clients.get(identity) is None
    response = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json=command(99)
    )
    assert response.status == 401
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 0
    assert stack.client.online


class AbsentBrowserModel(SingleSchema):
    x = NumericField(default=0)


@pytest.mark.asyncio
async def test_missing_schema_subscription_failure_is_captured_and_isolated(
    browser_stack: BrowserStack,
) -> None:
    from kivyfn.store.error import StoreSubscriptionError
    from kivyfn.store.state.manager import StateManager

    stack = browser_stack
    websocket, _, _ = await connect(stack)
    manager = StateManager(stack.client)
    healthy = manager.create(BrowserModel)
    await stack.client.connect()
    unavailable = manager.create(AbsentBrowserModel)
    with pytest.raises(StoreSubscriptionError) as error:
        await stack.client.connect()
    assert error.value.failure.code == "node_not_found"
    assert stack.client.online
    assert healthy.synced and not unavailable.synced
    facade = stack.host.devtools
    assert facade is not None
    records = facade.recorder.activity()["records"]
    requests = [
        record
        for record in records
        if record["category"] == "request"
        and any(
            item.get("schema") == "AbsentBrowserModel"
            for item in record["detail"]["request"]["requests"]
        )
    ]
    assert len(requests) == 1
    assert requests[0]["status"] == "received"
    response = next(
        record
        for record in records
        if record["category"] == "response"
        and record["capture_id"] == requests[0]["capture_id"]
    )
    assert response["status"] == "failed"
    assert (
        response["detail"]["response"]["responses"][0]["failure"]["code"]
        == "node_not_found"
    )
    assert not [
        record for record in records if record["category"] == "boundary"
    ]
    assert not facade.recorder.pending
    result = await healthy.set(x=7)
    assert result.success
    assert healthy.x == 7 and healthy.synced
    assert (await receive(websocket, "commit"))["payload"]["events"][0][
        "new"
    ] == 7
    manager.release(AbsentBrowserModel)
    manager.release(BrowserModel)
    assert stack.client.online
    await websocket.close()


@pytest.mark.asyncio
async def test_browser_command_and_canonical_commit_are_captured_once(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    other, _, _ = await connect(stack)
    logical_id = "record-once-browser-command"
    response = await stack.http.post(
        stack.url + "/api/commands",
        headers=headers,
        json=command(23, identity=logical_id),
    )
    assert (await response.json())["responses"][0]["type"] == "transaction"
    first_commit = await receive(websocket, "commit")
    other_commit = await receive(other, "commit")
    assert first_commit["payload"]["transaction_id"] == logical_id
    assert other_commit["payload"]["transaction_id"] == logical_id
    facade = stack.host.devtools
    assert facade is not None
    captured = [
        record
        for record in facade.recorder.activity()["records"]
        if record["transaction_id"] == logical_id
    ]
    assert [record["category"] for record in captured] == [
        "request",
        "commit",
        "response",
    ]
    assert captured[0]["capture_id"] == captured[2]["capture_id"]
    assert captured[0]["detail"]["request"] == command(23, identity=logical_id)
    assert len(stack.store.history().entries) == 1
    await websocket.close()
    await other.close()


@pytest.mark.asyncio
async def test_oversized_response_preserves_unknown_outcome_and_retry(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    facade = stack.host.devtools
    assert facade is not None
    facade.server.config = replace(
        facade.server.config, max_payload_bytes=4096
    )
    await stack.client.transaction(
        transaction_id="large-query-data",
        actions=(
            InsertRequestAction(
                "BrowserRows", "large", (("payload", "x" * 12_000),)
            ),
        ),
    )
    request = command(5, identity="mutate-then-read")
    request["requests"].append(
        {
            "type": "query",
            "query": {
                "version": 1,
                "source": {"schema": "BrowserRows"},
                "joins": [],
                "predicate": None,
            },
        }
    )
    first = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json=request
    )
    assert first.status == 502
    assert (await first.json())["outcome"] == "unknown"
    assert first.content_length is not None
    assert first.content_length <= facade.server.config.max_payload_bytes
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 5
    await stack.client.transaction(
        transaction_id="independent-write-after-unknown",
        actions=(SetRequestAction("BrowserModel", (("x", 7),)),),
    )
    retry = await stack.http.post(
        stack.url + "/api/commands", headers=headers, json=request
    )
    assert retry.status == 502
    assert (await retry.json())["outcome"] == "unknown"
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 7
    committed = [
        entry
        for entry in stack.store.history().entries
        if "mutate-then-read" in entry.transaction_ids
    ]
    assert len(committed) == 1
    # The existing Host cache still owns the compact transaction result.
    acknowledgement = await stack.http.post(
        stack.url + "/api/commands",
        headers=headers,
        json=command(5, identity="mutate-then-read"),
    )
    assert acknowledgement.status == 200
    assert (await acknowledgement.json())["responses"][0][
        "transaction_id"
    ] == "mutate-then-read"
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 7
    await websocket.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_id", ["$integer", "$object", "$unsupported", "__proto__", "2"]
)
async def test_structural_rows_and_tagged_values_across_diagnostic_views(
    browser_stack: BrowserStack, row_id: str
) -> None:
    stack = browser_stack
    literal = {"$integer": "a literal, not an integer"}
    await stack.client.transaction(
        transaction_id="tagged-row-insert",
        actions=(
            InsertRequestAction(
                "BrowserRows", row_id, (("x", 2**80), ("payload", literal))
            ),
        ),
    )
    websocket, initial, headers = await connect(stack)
    checkpoint = initial["payload"]["checkpoint"]
    rows = checkpoint["schemas"]["BrowserRows"]
    assert set(rows) == {row_id}
    assert rows[row_id]["x"] == {"$integer": str(2**80)}
    assert from_wire(rows[row_id]["payload"]) == literal
    response = await stack.http.get(stack.url + "/api/state", headers=headers)
    assert response.status == 200
    state = await response.json()
    assert state["schemas"]["BrowserRows"] == rows
    assert "row_orders" not in state
    changed_literal = {"$object": [["literal", {"$integer": "still literal"}]]}
    await stack.client.transaction(
        transaction_id="tagged-row-change",
        actions=(
            SetRowRequestAction(
                "BrowserRows",
                row_id,
                (("x", 2**81), ("payload", changed_literal)),
            ),
        ),
    )
    commit = (await receive(websocket, "commit"))["payload"]
    assert "row_orders" not in commit
    activity = await stack.http.get(
        stack.url + "/api/activity", headers=headers
    )
    record = next(
        record
        for record in (await activity.json())["records"]
        if record["category"] == "commit"
        and record["transaction_id"] == "tagged-row-change"
    )
    assert record["detail"] == commit
    preview = await stack.http.get(
        stack.url + f"/api/preview?sequence={commit['sequence']}",
        headers=headers,
    )
    assert preview.status == 200
    result = await preview.json()
    assert result["available"]
    row = result["checkpoint"]["schemas"]["BrowserRows"][row_id]
    assert row["x"] == {"$integer": str(2**81)}
    assert from_wire(row["payload"]) == changed_literal
    assert "row_orders" not in result["checkpoint"]
    await stack.client.undo(transaction_id="tagged-row-undo")
    assert stack.store.snapshot_by_schema("BrowserRows")[row_id] == {
        "x": 2**80,
        "payload": literal,
    }
    await websocket.close()


@pytest.mark.asyncio
async def test_signal_only_capture_includes_schema_and_compensation(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    response = await stack.http.post(
        stack.url + "/api/capture",
        headers=headers,
        json={"filters": {"schema": "BrowserModel", "category": "commit"}},
    )
    assert response.status == 200
    await stack.client.transaction(
        transaction_id="signal-only",
        actions=(SignalRequestAction("BrowserModel", "open_window", {}),),
    )
    await stack.client.undo(transaction_id="signal-undo")
    await stack.client.redo(transaction_id="signal-redo")
    activity = await stack.http.get(
        stack.url + "/api/activity", headers=headers
    )
    records = (await activity.json())["records"]
    selected = [
        record
        for record in records
        if record["transaction_id"]
        in {"signal-only", "signal-undo", "signal-redo"}
    ]
    assert len(selected) == 3
    assert [record["detail"]["kind"] for record in selected] == [
        "transaction",
        "undo",
        "redo",
    ]
    for record in selected:
        assert record["schemas"] == ["BrowserModel"]
        assert record["detail"]["schema_revisions"] == {}
        assert record["detail"]["events"] == []
    assert [
        record["detail"]["signals"][0]["signal"] for record in selected
    ] == ["open_window", "close_window", "open_window"]
    await websocket.close()


@pytest.mark.asyncio
async def test_shared_capture_controls_reach_both_sessions_and_reconnect(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    first, _, headers = await connect(stack)
    second, _, _ = await connect(stack)
    controls = [
        {"paused": True, "filters": {}},
        {"paused": True, "filters": {"client_id": "part", "identity": "tx"}},
        {"paused": False, "filters": {"schema": "BrowserModel"}},
    ]
    for control in controls:
        response = await stack.http.post(
            stack.url + "/api/capture", headers=headers, json=control
        )
        assert response.status == 200
        for websocket in (first, second):
            capture = (await receive(websocket, "capture"))["payload"]
            assert capture["paused"] == control["paused"]
            assert capture["filters"] == control["filters"]
            assert capture["records"] == []
    await second.close()
    replacement, snapshot, _ = await connect(stack)
    capture = snapshot["payload"]["activity"]
    assert capture["paused"] is False
    assert capture["filters"] == {"schema": "BrowserModel"}
    await first.close()
    await replacement.close()


@pytest.mark.asyncio
async def test_devtools_access_is_quiet_and_diagnostic_failures_are_logged(
    browser_stack: BrowserStack,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    stack = browser_stack
    caplog.set_level(logging.INFO, logger="aiohttp.access")
    for path in ("/", "/api/info"):
        response = await stack.http.get(stack.url + path)
        assert response.status == 200
        await response.read()
    assert not any(
        record.name == "aiohttp.access"
        and (
            '"GET / HTTP' in record.getMessage()
            or '"GET /api/info HTTP' in record.getMessage()
        )
        for record in caplog.records
    )
    facade = stack.host.devtools
    assert facade is not None

    def broken(encoded_commit: dict[str, Any]) -> None:
        raise RuntimeError("Injected capture failure remains visible")

    monkeypatch.setattr(facade.recorder, "commit", broken)
    await stack.client.transaction(
        transaction_id="diagnostic-error",
        actions=(SetRequestAction("BrowserModel", (("x", 8),)),),
    )
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 8
    assert any(
        record.getMessage() == "Devtools commit capture failed"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_adopted_partial_capture_filters_keep_future_matching_commits(
    browser_stack: BrowserStack,
) -> None:
    stack = browser_stack
    websocket, _, headers = await connect(stack)
    response = await stack.http.post(
        stack.url + "/api/commands",
        headers=headers,
        json=command(1, identity="original-match"),
    )
    assert response.status == 200
    response = await stack.http.post(
        stack.url + "/api/capture",
        headers=headers,
        json={
            "filters": {
                "client_id": headers["X-DevTools-Session"][2:10],
                "identity": "match",
                "schema": "BrowserModel",
                "category": "commit",
                "status": "ok",
            }
        },
    )
    assert response.status == 200
    for identity, value in (("future-match", 2), ("excluded", 3)):
        response = await stack.http.post(
            stack.url + "/api/commands",
            headers=headers,
            json=command(value, identity=identity),
        )
        assert response.status == 200
    response = await stack.http.get(
        stack.url + "/api/activity", headers=headers
    )
    records = (await response.json())["records"]
    assert [
        record["transaction_id"]
        for record in records
        if record["category"] == "commit"
    ] == ["original-match", "future-match"]
    assert stack.store.snapshot_by_schema("BrowserModel")["x"] == 3
    await websocket.close()
