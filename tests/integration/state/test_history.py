from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from kivy.properties import BooleanProperty

from kivyfn.store.builder.config import HostConfig
from kivyfn.store.client.client import StoreClient
from kivyfn.store.host.host import StoreHost
from kivyfn.store.state.template import state_template
from kivyfn.store.store.store import Store
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.builder.config import HistoryConfig
from kivyfn.store.events import StoreCommit
from kivyfn.store.protocol.message.requests import (
    InsertRequestAction,
    RemoveRequestAction,
    RequestBatch,
    SetRowRequestAction,
    SignalRowRequestAction,
)
from kivyfn.store.protocol.message.responses import (
    ResponseBatch,
    TransactionResponse,
)
from kivyfn.store.query import Query, field
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.fields import ForeignKeyField, NumericField
from kivyfn.store.schema.signal import Signal
from kivyfn.store.state.manager import StateManager
from kivyfn.store.typing import StateType
from tests.conftest import TransportKind, _client_endpoint, _host_endpoint


class ReactiveHistoryParent(ManySchema):
    track_history = True
    value = NumericField(default=0)


class ReactiveHistoryRows(ManySchema):
    track_history = True
    value = NumericField(default=0)
    parent_id = ForeignKeyField(schema="ReactiveHistoryParent")
    ping = Signal(undo="unping")
    unping = Signal(undo="ping")


class HistoryRow(StateType):
    selected = BooleanProperty(False)


@dataclass
class HistoryStack:
    store: Store
    host: StoreHost
    client: StoreClient
    peer: StoreClient
    manager: StateManager


@asynccontextmanager
async def history_stack(
    path: Path, transport: TransportKind
) -> AsyncIterator[HistoryStack]:
    tree = Branch(ReactiveHistoryRows).child(
        "parents", Branch(ReactiveHistoryParent)
    )
    config = Builder().tree(tree).storage_path(path).build()
    store = Store(replace(config, history=HistoryConfig()))
    host = StoreHost(store, HostConfig(_host_endpoint(transport)))
    client = StoreClient()
    peer = StoreClient()
    try:
        store.boot()
        await host.start()
        yield HistoryStack(store, host, client, peer, StateManager(client))
    finally:
        await client.disconnect()
        await peer.disconnect()
        await host.stop()
        store.shutdown()


async def seed(stack: HistoryStack) -> None:
    response = await stack.client.transaction(
        transaction_id="seed",
        actions=tuple(
            InsertRequestAction("ReactiveHistoryRows", row_id, (("value", 1),))
            for row_id in ("a", "b", "c")
        ),
    )
    assert isinstance(response, TransactionResponse)


@pytest.mark.asyncio
async def test_keyed_replacement_navigation_keeps_local_state_identity(
    tmp_path: Path, transport_kind: TransportKind
) -> None:
    async with history_stack(tmp_path, transport_kind) as stack:
        rows = stack.manager.create(
            state_template(ReactiveHistoryRows, state_type=HistoryRow)
        )
        query = stack.manager.create(
            state_template(Query(ReactiveHistoryRows), state_type=HistoryRow)
        )
        await stack.client.connect(_client_endpoint(stack.host))
        await seed(stack)
        row, projected = rows["a"], query["a"]
        row.selected = projected.selected = True
        response = await stack.client.transaction(
            transaction_id="replace-a",
            actions=(
                RemoveRequestAction("ReactiveHistoryRows", "a"),
                InsertRequestAction(
                    "ReactiveHistoryRows", "a", (("value", 2),)
                ),
            ),
        )
        assert isinstance(response, TransactionResponse)
        assert set(rows) == set(query) == {"a", "b", "c"}
        membership = []
        for target in (rows, query):
            target.bind(on_enter=lambda *args: membership.append(args))
            target.bind(on_leave=lambda *args: membership.append(args))
            target.bind(rows=lambda *args: membership.append(args))
        commits: list[StoreCommit] = []
        unsubscribe = stack.store.subscribe_commits(commits.append)
        try:
            before = stack.store.schema_revisions["ReactiveHistoryRows"]
            assert isinstance(await stack.client.undo(), TransactionResponse)
            assert row.get("value") == projected.get("value") == 1
            assert isinstance(await stack.client.redo(), TransactionResponse)
            assert row.get("value") == projected.get("value") == 2
            assert stack.store.schema_revisions["ReactiveHistoryRows"] == (
                before + 2
            )
        finally:
            unsubscribe()
        assert not membership
        assert set(rows) == set(query) == {"a", "b", "c"}
        assert rows["a"] is row and query["a"] is projected
        assert row.selected and projected.selected
        assert [commit.kind for commit in commits] == ["undo", "redo"]
        assert all(commit.events for commit in commits)


@pytest.mark.asyncio
async def test_structural_navigation_and_signals_use_final_query_membership(
    tmp_path: Path, transport_kind: TransportKind
) -> None:
    async with history_stack(tmp_path, transport_kind) as stack:
        rows = stack.manager.create(
            state_template(ReactiveHistoryRows, state_type=HistoryRow)
        )
        query = stack.manager.create(
            Query(ReactiveHistoryRows).where(field("value") == 1)
        )
        await stack.client.connect(_client_endpoint(stack.host))
        await seed(stack)
        removed = rows["b"]
        removed.selected = True
        signals = []
        rows.bind(
            on_unping=lambda _, row_id, data: signals.append(
                ("rows", row_id, data, rows.keys(), query.keys())
            )
        )
        query.bind(
            on_unping=lambda _, row_id, data: signals.append(
                ("query", row_id, data, rows.keys(), query.keys())
            )
        )
        reply = await stack.client.transaction(
            transaction_id="delete-and-signal",
            actions=(
                RemoveRequestAction("ReactiveHistoryRows", "b"),
                SetRowRequestAction(
                    "ReactiveHistoryRows", "a", (("value", 2),)
                ),
                SignalRowRequestAction(
                    "ReactiveHistoryRows", "a", "ping", {"step": 1}
                ),
                SignalRowRequestAction(
                    "ReactiveHistoryRows", "a", "ping", {"step": 2}
                ),
            ),
        )
        assert isinstance(reply, TransactionResponse)
        assert set(rows) == {"a", "c"} and set(query) == {"c"}
        membership = []
        for target in (rows, query):
            target.bind(
                rows=lambda *_: membership.append((set(rows), set(query)))
            )
        assert isinstance(await stack.client.undo(), TransactionResponse)
        assert set(rows) == set(query) == {"a", "b", "c"}
        assert membership
        assert all(
            ids == projected == {"a", "b", "c"}
            for ids, projected in membership
        )
        assert rows["b"] is removed and removed.selected
        assert [
            data["step"] for name, _, data, _, _ in signals if name == "rows"
        ] == [2, 1]
        assert [
            data["step"] for name, _, data, _, _ in signals if name == "query"
        ] == [2, 1]
        assert all(
            set(ids) == set(projected) == {"a", "b", "c"}
            for _, _, _, ids, projected in signals
        )
        membership.clear()
        assert isinstance(await stack.client.redo(), TransactionResponse)
        assert set(rows) == {"a", "c"} and set(query) == {"c"}
        assert membership
        assert all(
            ids == {"a", "c"} and projected == {"c"}
            for ids, projected in membership
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_pending", [False, True])
async def test_foreign_undo_updates_canonical_beneath_pending_overlay(
    tmp_path: Path,
    transport_kind: TransportKind,
    monkeypatch: pytest.MonkeyPatch,
    fail_pending: bool,
) -> None:
    async with history_stack(tmp_path, transport_kind) as stack:
        rows = stack.manager.create(ReactiveHistoryRows)
        await stack.client.connect(_client_endpoint(stack.host))
        await stack.peer.connect(_client_endpoint(stack.host))
        await seed(stack)
        await rows["a"].set(value=2)
        started = asyncio.Event()
        release = asyncio.Event()
        delivered = asyncio.Event()
        transport = stack.client._transport
        assert transport is not None
        original_request = transport.request
        original_message = stack.client.on_message

        async def gated(request: RequestBatch) -> ResponseBatch:
            started.set()
            await release.wait()
            if fail_pending:
                raise ConnectionError("Injected pending request failure")
            return await original_request(request)

        def observed_message(message):
            original_message(message)
            delivered.set()

        monkeypatch.setattr(transport, "request", gated)
        monkeypatch.setattr(stack.client, "on_message", observed_message)
        with stack.manager.transaction() as pending:
            rows["a"].value = 9
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert isinstance(await stack.peer.undo(), TransactionResponse)
            await asyncio.wait_for(delivered.wait(), 2)
            assert rows["a"].get("value") == 9
            assert rows["a"].canonical_snapshot({"value"}) == {"value": 1}
            assert rows["a"].is_pending
        finally:
            release.set()
        await pending
        assert pending.failed is fail_pending
        assert rows["a"].get("value") == (1 if fail_pending else 9)
        assert not rows["a"].is_pending


@pytest.mark.asyncio
async def test_state_group_metadata_merges_live_commits_into_one_entry(
    tmp_path: Path, transport_kind: TransportKind
) -> None:
    async with history_stack(tmp_path, transport_kind) as stack:
        rows = stack.manager.create(ReactiveHistoryRows)
        await stack.client.connect(_client_endpoint(stack.host))
        await seed(stack)
        before = stack.store.history().status.total_entries
        for value in (2, 3):
            with stack.manager.transaction(
                history_group="drag-1", history_label="Move row"
            ) as transaction:
                rows["a"].value = value
            await transaction
            assert transaction.success
        page = stack.store.history()
        assert page.status.total_entries == before + 1
        assert isinstance(await stack.client.undo(), TransactionResponse)
        assert rows["a"].get("value") == 1
        assert isinstance(await stack.client.redo(), TransactionResponse)
        assert rows["a"].get("value") == 3


@pytest.mark.asyncio
async def test_joined_projected_writes_and_nullable_relation_restore(
    tmp_path: Path, transport_kind: TransportKind
) -> None:
    async with history_stack(tmp_path, transport_kind) as stack:
        parents = stack.manager.create(ReactiveHistoryParent)
        rows = stack.manager.create(ReactiveHistoryRows)
        source = Query(ReactiveHistoryRows).left_join(
            "parent_id", as_="parent"
        )
        joined = stack.manager.create(
            state_template(source, state_type=HistoryRow)
        )
        await stack.client.connect(_client_endpoint(stack.host))
        response = await stack.client.transaction(
            transaction_id="joined-seed",
            actions=(
                InsertRequestAction(
                    "ReactiveHistoryParent", "p", (("value", 10),)
                ),
                InsertRequestAction(
                    "ReactiveHistoryRows", "a", (("parent_id", "p"),)
                ),
            ),
        )
        assert isinstance(response, TransactionResponse)
        row = joined["a"]
        row.selected = True
        await row.set(parent_value=20)
        assert parents["p"].get("value") == row.get("parent_value") == 20
        assert isinstance(await stack.client.undo(), TransactionResponse)
        assert parents["p"].get("value") == row.get("parent_value") == 10
        assert isinstance(await stack.client.redo(), TransactionResponse)
        assert parents["p"].get("value") == row.get("parent_value") == 20
        await row.set(parent_id=None)
        assert rows["a"].get("parent_id") is None
        assert row.get("parent_value") is None
        assert isinstance(await stack.client.undo(), TransactionResponse)
        assert rows["a"].get("parent_id") == "p"
        assert row.get("parent_value") == 20
        assert joined["a"] is row and row.selected
        equivalent = Query(ReactiveHistoryRows).left_join(
            "parent_id", as_="parent"
        )
        assert stack.manager.create(equivalent) is joined


@pytest.mark.asyncio
async def test_compensation_callback_new_tracked_work_is_separate_history(
    tmp_path: Path, transport_kind: TransportKind
) -> None:
    async with history_stack(tmp_path, transport_kind) as stack:
        rows = stack.manager.create(ReactiveHistoryRows)
        await stack.client.connect(_client_endpoint(stack.host))
        await seed(stack)
        with stack.manager.transaction(
            history_group="gesture", history_label="Original command"
        ) as transaction:
            rows["a"].set(value=2)
            rows["a"].ping()
        await transaction
        submitted = []
        rows["a"].bind(
            on_unping=lambda *_: submitted.append(rows["a"].set(value=50))
        )
        assert isinstance(await stack.client.undo(), TransactionResponse)
        assert len(submitted) == 1
        await submitted[0]
        assert submitted[0].success
        assert rows["a"].get("value") == 50
        history = stack.store.history()
        assert not history.status.can_redo
        assert history.entries[-1].group is None
        assert history.entries[-1].transaction_ids == (submitted[0].id,)
