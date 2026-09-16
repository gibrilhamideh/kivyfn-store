"""History navigation and retry invariants across both real transports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from kivyfn.store import HostConfig, Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.builder.config import HistoryConfig
from kivyfn.store.error import StoreErrorCode, StoreStateError
from kivyfn.store.protocol.message.requests import (
    SetRequestAction,
    SignalRequestAction,
    TransactionRequest,
)
from kivyfn.store.protocol.message.responses import (
    HistoryResponse,
    TransactionFailureResponse,
    TransactionResponse,
)
from kivyfn.store.schema import NumericField, Signal, SingleSchema
from tests.conftest import TransportKind


class NavigationSchema(SingleSchema):
    track_history = True
    value = NumericField(default=0)
    open = Signal[dict[str, str]](undo="close")
    close = Signal[dict[str, str]](undo="open")


@dataclass
class NavigationStack:
    store: Store
    host: StoreHost
    first: StoreClient
    second: StoreClient
    endpoint: str


@pytest_asyncio.fixture
async def navigation_stack(
    tmp_path: Path, transport_kind: TransportKind
) -> AsyncIterator[NavigationStack]:
    config = (
        Builder().tree(Branch(NavigationSchema)).storage_path(tmp_path).build()
    )
    store = Store(replace(config, history=HistoryConfig()))
    host = StoreHost(
        store,
        HostConfig(
            f"direct://history-{uuid4().hex}"
            if transport_kind == "direct"
            else "http://127.0.0.1:0"
        ),
    )
    first, second = StoreClient(), StoreClient()
    try:
        store.boot()
        await host.start()
        endpoint = host.transport.endpoint.get("endpoint", host.endpoint)
        await first.connect(endpoint)
        await second.connect(endpoint)
        yield NavigationStack(store, host, first, second, endpoint)
    finally:
        await first.disconnect()
        await second.disconnect()
        await host.stop()
        store.shutdown()


async def set_value(
    client: StoreClient, value: int, *, group: str | None = None
) -> TransactionResponse:
    reply = await client.transaction(
        transaction_id=uuid4().hex,
        actions=(SetRequestAction("NavigationSchema", (("value", value),)),),
        history_group=group,
        history_label="Value gesture" if group else None,
    )
    assert isinstance(reply, TransactionResponse)
    return reply


@pytest.mark.asyncio
async def test_history_roundtrip_group_and_strict_navigation(
    navigation_stack: NavigationStack,
) -> None:
    stack = navigation_stack
    await set_value(stack.first, 1, group="gesture")
    await set_value(stack.first, 2, group="gesture")
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    assert len(history.page.entries) == 1
    entry = history.page.entries[0]
    assert entry.label == "Value gesture"
    assert len(entry.transaction_ids) == 2
    assert entry.records[0].before == 0
    assert entry.records[0].after == 2
    revision = stack.store.schema_revisions["NavigationSchema"]
    assert isinstance(await stack.first.undo(), TransactionResponse)
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 0
    assert stack.store.schema_revisions["NavigationSchema"] == revision + 1
    assert isinstance(await stack.second.redo(), TransactionResponse)
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 2


@pytest.mark.asyncio
async def test_same_id_retry_survives_reconnect_before_stale_check(
    navigation_stack: NavigationStack,
) -> None:
    stack = navigation_stack
    await set_value(stack.first, 1)
    await set_value(stack.first, 2)
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    position = history.page.status.undo_position
    first = await stack.first.undo(
        transaction_id="logical-undo", expected_position=position
    )
    assert isinstance(first, TransactionResponse)
    await stack.first.disconnect()
    await stack.first.connect(stack.endpoint)
    repeated = await stack.first.undo(
        transaction_id="logical-undo", expected_position=position
    )
    assert repeated == first
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 1
    stale = await stack.second.undo(
        transaction_id="different-undo", expected_position=position
    )
    assert isinstance(stale, TransactionFailureResponse)
    assert stale.failure.code == StoreErrorCode.HISTORY_STALE
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 1


@pytest.mark.asyncio
async def test_cache_eviction_cannot_undo_next_entry(
    navigation_stack: NavigationStack,
) -> None:
    stack = navigation_stack
    stack.host.requests._max_completed = 1
    await set_value(stack.first, 1)
    await set_value(stack.first, 2)
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    position = history.page.status.undo_position
    assert isinstance(
        await stack.first.undo(
            transaction_id="evicted", expected_position=position
        ),
        TransactionResponse,
    )
    assert isinstance(await stack.first.redo(), TransactionResponse)
    reply = await stack.first.undo(
        transaction_id="evicted", expected_position=position
    )
    assert isinstance(reply, TransactionFailureResponse)
    assert reply.failure.code == StoreErrorCode.HISTORY_STALE
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["group", "label", "guards", "kind"])
async def test_reused_identity_fingerprints_all_metadata(
    navigation_stack: NavigationStack, changed: str
) -> None:
    stack = navigation_stack
    actions = (SetRequestAction("NavigationSchema", (("value", 1),)),)
    reply = await stack.first.transaction(
        transaction_id="same", actions=actions
    )
    assert isinstance(reply, TransactionResponse)
    if changed == "kind":
        second = await stack.first.undo(transaction_id="same")
    else:
        request = TransactionRequest("same", actions)
        if changed == "group":
            request = replace(request, history_group="different")
        elif changed == "label":
            request = replace(request, history_label="different")
        else:
            request = replace(
                request, expected_revisions={"NavigationSchema": 1}
            )
        second = await stack.first.transaction(
            transaction_id=request.transaction_id,
            actions=request.actions,
            history_group=request.history_group,
            history_label=request.history_label,
            expected_revisions=request.expected_revisions,
        )
    assert isinstance(second, TransactionFailureResponse)
    assert second.failure.code == StoreErrorCode.INVALID_OPERATION
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 1


@pytest.mark.asyncio
async def test_cancelled_mutation_waiter_still_blocks_navigation(
    navigation_stack: NavigationStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = navigation_stack
    entered, release = asyncio.Event(), asyncio.Event()
    original = stack.host.request

    async def delayed(client_id, batch):
        response = await original(client_id, batch)
        entered.set()
        await release.wait()
        return response

    monkeypatch.setattr(stack.host, "request", delayed)
    pending = stack.first.transaction(
        transaction_id="cancelled-waiter",
        actions=(SetRequestAction("NavigationSchema", (("value", 1),)),),
    )
    await entered.wait()
    pending.cancel()
    try:
        with pytest.raises(StoreStateError, match="pending local mutations"):
            await stack.first.undo()
    finally:
        release.set()
        flush = stack.first._flush_task
        assert flush is not None
        await flush
    assert isinstance(await stack.first.undo(), TransactionResponse)


@pytest.mark.asyncio
async def test_status_fetch_race_rechecks_local_pending_work(
    navigation_stack: NavigationStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = navigation_stack
    await set_value(stack.first, 1)
    original = stack.first.history
    pending = []

    def raced_history(**kwargs):
        result = original(**kwargs)
        pending.append(
            stack.first.transaction(
                transaction_id="raced-mutation",
                actions=(
                    SetRequestAction("NavigationSchema", (("value", 2),)),
                ),
            )
        )
        return result

    entered, release = asyncio.Event(), asyncio.Event()
    handle = stack.host.request

    async def gated(client_id, batch):
        result = await handle(client_id, batch)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(stack.first, "history", raced_history)
    monkeypatch.setattr(stack.host, "request", gated)
    navigation = asyncio.create_task(stack.first.undo())
    await entered.wait()
    release.set()
    # Both requests share a batch; mutation has settled by response delivery,
    # so stale guarded navigation must reject instead of acting on newer work.
    result = await navigation
    assert isinstance(result, TransactionFailureResponse)
    assert result.failure.code == StoreErrorCode.HISTORY_STALE
    assert pending
    await pending[0]
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 2


@pytest.mark.asyncio
async def test_inverse_signal_retry_is_not_republished(
    navigation_stack: NavigationStack,
) -> None:
    stack = navigation_stack
    commits = []
    remove = stack.store.subscribe_commits(commits.append)
    try:
        result = await stack.first.transaction(
            transaction_id="open-command",
            actions=(
                SignalRequestAction(
                    "NavigationSchema", "open", {"owner": "app-window"}
                ),
            ),
        )
        assert isinstance(result, TransactionResponse)
        history = await stack.first.history()
        assert isinstance(history, HistoryResponse)
        signal = history.page.entries[0].signals[0]
        assert signal.forward.signal == "open"
        assert signal.inverse.signal == "close"
        position = history.page.status.undo_position
        assert isinstance(
            await stack.second.undo(
                transaction_id="close-command", expected_position=position
            ),
            TransactionResponse,
        )
        await stack.first.disconnect()
        await stack.second.undo(
            transaction_id="close-command", expected_position=position
        )
        assert [commit.signals[0].signal for commit in commits] == [
            "open",
            "close",
        ]
        assert stack.store.schema_revisions["NavigationSchema"] == 0
    finally:
        remove()


@pytest.mark.asyncio
async def test_pending_mutation_created_during_history_fetch_is_rejected(
    navigation_stack: NavigationStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = navigation_stack
    await set_value(stack.first, 1)
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    fetched = asyncio.Event()
    status = asyncio.get_running_loop().create_future()

    def history_gate(**kwargs):
        fetched.set()
        return status

    monkeypatch.setattr(stack.first, "history", history_gate)
    navigating = asyncio.create_task(stack.first.undo())
    await fetched.wait()
    # Queue after status began, before the convenience method resumes.
    pending = stack.first.transaction(
        transaction_id="queued-during-fetch",
        actions=(SetRequestAction("NavigationSchema", (("value", 2),)),),
    )
    status.set_result(history)
    with pytest.raises(StoreStateError, match="pending local mutations"):
        await navigating
    await pending
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_id", "wrong_type"])
async def test_invalid_navigation_response_disconnects_and_settles_waiter(
    navigation_stack: NavigationStack,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from kivyfn.store.error import StoreProtocolError
    from kivyfn.store.protocol.message.responses import ResponseBatch

    stack = navigation_stack
    await set_value(stack.first, 1)
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    handle = stack.host.requests.handle

    def malformed(client_id, batch):
        handle(client_id, batch)
        reply = (
            TransactionResponse("wrong") if fault == "wrong_id" else history
        )
        return ResponseBatch((reply,))

    monkeypatch.setattr(stack.host.requests, "handle", malformed)
    with pytest.raises(StoreProtocolError):
        await stack.first.undo(
            expected_position=history.page.status.undo_position
        )
    assert not stack.first.online
    assert stack.first._futures.empty
    # Protocol failure after commit is ambiguous, never automatically retried.
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 0


@pytest.mark.asyncio
async def test_lost_undo_reply_retries_same_identity_without_second_commit(
    navigation_stack: NavigationStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kivyfn.store.error import StoreConnectionError

    stack = navigation_stack
    await set_value(stack.first, 1)
    await set_value(stack.first, 2)
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    position = history.page.status.undo_position
    transport = stack.first._require_transport()
    request = transport.request
    commits = []
    remove = stack.store.subscribe_commits(commits.append)

    async def lost_reply(batch):
        await request(batch)
        raise StoreConnectionError("Reply lost after commit")

    monkeypatch.setattr(transport, "request", lost_reply)
    try:
        with pytest.raises(StoreConnectionError, match="Reply lost"):
            await stack.first.undo(
                transaction_id="lost-undo", expected_position=position
            )
        assert not stack.first.online
        assert len(commits) == 1
        monkeypatch.setattr(transport, "request", request)
        await stack.first.connect(stack.endpoint)
        result = await stack.first.undo(
            transaction_id="lost-undo", expected_position=position
        )
        assert isinstance(result, TransactionResponse)
        assert len(commits) == 1
        assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 1
    finally:
        remove()


@pytest.mark.asyncio
async def test_two_clients_navigating_same_position_cannot_skip_an_entry(
    navigation_stack: NavigationStack,
) -> None:
    stack = navigation_stack
    await set_value(stack.first, 1)
    await set_value(stack.first, 2)
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    position = history.page.status.undo_position
    replies = await asyncio.gather(
        stack.first.undo(expected_position=position),
        stack.second.undo(expected_position=position),
    )
    successful = [
        reply for reply in replies if isinstance(reply, TransactionResponse)
    ]
    failures = [
        reply
        for reply in replies
        if isinstance(reply, TransactionFailureResponse)
    ]
    assert len(successful) == len(failures) == 1
    assert failures[0].failure.code == StoreErrorCode.HISTORY_STALE
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 1


@pytest.mark.asyncio
async def test_cancelled_undo_waiter_remains_inflight_until_response(
    navigation_stack: NavigationStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = navigation_stack
    await set_value(stack.first, 1)
    history = await stack.first.history()
    assert isinstance(history, HistoryResponse)
    position = history.page.status.undo_position
    entered, release = asyncio.Event(), asyncio.Event()
    handle = stack.host.request

    async def gated(client_id, batch):
        result = await handle(client_id, batch)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(stack.host, "request", gated)
    navigating = asyncio.create_task(
        stack.first.undo(
            transaction_id="cancelled-undo", expected_position=position
        )
    )
    await entered.wait()
    navigating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await navigating
    try:
        with pytest.raises(StoreStateError, match="pending local mutations"):
            await stack.first.redo()
    finally:
        release.set()
        flush = stack.first._flush_task
        assert flush is not None
        await flush
    assert isinstance(
        await stack.first.undo(
            transaction_id="cancelled-undo", expected_position=position
        ),
        TransactionResponse,
    )
    assert stack.store.snapshot_by_schema("NavigationSchema")["value"] == 0


@pytest.mark.asyncio
async def test_stable_owner_filter_and_no_compensation_replay_after_reconnect(
    navigation_stack: NavigationStack,
) -> None:
    from kivyfn.store.state.manager import StateManager

    stack = navigation_stack
    managers = (StateManager(stack.first), StateManager(stack.second))
    states = tuple(manager.create(NavigationSchema) for manager in managers)
    observed: list[list[tuple[str, str]]] = [[], []]
    handled: list[list[str]] = [[], []]
    arrived = (asyncio.Event(), asyncio.Event())

    def handler(index: int, command: str):
        def receive(_, payload):
            owner = payload["owner"]
            observed[index].append((command, owner))
            if owner == ("window-a", "window-b")[index]:
                handled[index].append(command)
            arrived[index].set()

        return receive

    for index, state in enumerate(states):
        state.bind(
            on_open=handler(index, "open"), on_close=handler(index, "close")
        )
    await stack.first.connect(stack.endpoint)
    await stack.second.connect(stack.endpoint)
    reply = await stack.first.transaction(
        transaction_id="owned-window-command",
        actions=(
            SignalRequestAction(
                "NavigationSchema", "open", {"owner": "window-a"}
            ),
        ),
    )
    assert isinstance(reply, TransactionResponse)
    await arrived[1].wait()
    assert observed == [[("open", "window-a")], [("open", "window-a")]]
    assert handled == [["open"], []]

    # Origin attribution must not target a disconnected owner for compensation.
    await stack.first.disconnect()
    assert isinstance(await stack.second.undo(), TransactionResponse)
    assert observed[1][-1] == ("close", "window-a")
    assert handled == [["open"], []]
    await stack.first.connect(stack.endpoint)
    assert states[0].synced
    assert observed[0] == [("open", "window-a")]
    assert handled == [["open"], []]

    # New signals reach the same stable owner after its transport ID changes.
    for command, navigate in (
        ("open", stack.second.redo),
        ("close", stack.second.undo),
    ):
        arrived[0].clear()
        assert isinstance(await navigate(), TransactionResponse)
        await arrived[0].wait()
        assert observed[0][-1] == (command, "window-a")
    assert handled == [["open", "open", "close"], []]
