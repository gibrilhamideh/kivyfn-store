from __future__ import annotations

from pathlib import Path

import pytest

from kivyfn.store import Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.operations import OpCode
from kivyfn.store.schema import SingleSchema
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.fields import NumericField
from kivyfn.store.schema.signal import Signal
from kivyfn.store.schema.trigger import trigger
from kivyfn.store.state.manager import StateManager
from kivyfn.store.transaction import Transaction
from tests.conftest import TransportKind, _client_endpoint, _host_endpoint


class InstructionOrderSource(SingleSchema):
    value = NumericField(default=0)
    ping = Signal()


class InstructionOrderObserver(SingleSchema):
    tick = NumericField(default=0)
    observed = NumericField(default=0)
    ping = Signal()

    @trigger(OpCode.SET, field="tick")
    def capture_source(
        self, transaction: Transaction, context: TriggerContext
    ) -> None:
        source = context.engine.get_node_by_schema("InstructionOrderSource")
        value = context.single(source)["value"]
        transaction.set(context.node.address, "observed", value)


@pytest.mark.asyncio
@pytest.mark.parametrize("optimistic", [False, True])
async def test_singleton_trigger_reads_preceding_write_not_later_coalesced_one(
    tmp_path: Path, transport_kind: TransportKind, optimistic: bool
) -> None:
    tree = Branch(InstructionOrderSource).child(
        "observer", Branch(InstructionOrderObserver)
    )
    root, config = Builder().tree(tree).storage_path(tmp_path).build()
    store = Store(root, config)
    host = StoreHost(store, _host_endpoint(transport_kind))
    client: StoreClient | None = None
    try:
        store.boot()
        await host.start()
        client = StoreClient()
        manager = StateManager(client)
        source = manager.create(InstructionOrderSource)
        observer = manager.create(InstructionOrderObserver)
        await client.connect(_client_endpoint(host))
        with manager.transaction() as tx:
            if optimistic:
                source.value = 1
                observer.tick = 1
                source.value = 2
            else:
                source.set(value=1)
                observer.set(tick=1)
                source.set(value=2)
        await tx
        assert tx.success
        assert source.get("value") == 2
        assert observer.get("observed") == 1
    finally:
        if client is not None:
            await client.disconnect()
        await host.stop()
        store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("two_managers", [False, True])
async def test_interleaved_signals_preserve_commit_order_after_all_bindings(
    tmp_path: Path, transport_kind: TransportKind, two_managers: bool
) -> None:
    from kivyfn.store.protocol.message.requests import (
        SetRequestAction,
        SignalRequestAction,
    )
    from kivyfn.store.protocol.message.responses import TransactionResponse

    tree = Branch(InstructionOrderSource).child(
        "observer", Branch(InstructionOrderObserver)
    )
    root, config = Builder().tree(tree).storage_path(tmp_path).build()
    store = Store(root, config)
    host = StoreHost(store, _host_endpoint(transport_kind))
    client: StoreClient | None = None
    try:
        store.boot()
        await host.start()
        client = StoreClient()
        first = StateManager(client)
        second = StateManager(client) if two_managers else first
        source = first.create(InstructionOrderSource)
        observer = second.create(InstructionOrderObserver)
        await client.connect(_client_endpoint(host))
        recorded = []
        source.bind(value=lambda *_: recorded.append(("retained", "source")))
        observer.bind(
            observed=lambda *_: recorded.append(("retained", "observer"))
        )

        def on_ping(_, data):
            assert source.get("value") == 1
            assert observer.get("observed") == 2
            recorded.append(("signal", data))

        source.bind(on_ping=on_ping)
        observer.bind(on_ping=on_ping)
        response = await client.transaction(
            transaction_id="signal-order",
            actions=(
                SignalRequestAction("InstructionOrderSource", "ping", 1),
                SetRequestAction("InstructionOrderSource", (("value", 1),)),
                SignalRequestAction("InstructionOrderObserver", "ping", 2),
                SetRequestAction(
                    "InstructionOrderObserver", (("observed", 2),)
                ),
                SignalRequestAction("InstructionOrderSource", "ping", 3),
            ),
        )
        assert isinstance(response, TransactionResponse)
        assert set(recorded[:2]) == {
            ("retained", "source"),
            ("retained", "observer"),
        }
        assert recorded[2:] == [("signal", 1), ("signal", 2), ("signal", 3)]
    finally:
        if client is not None:
            await client.disconnect()
        await host.stop()
        store.shutdown()
