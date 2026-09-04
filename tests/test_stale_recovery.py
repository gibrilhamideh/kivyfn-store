from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

from kivyfn.store.adapter.many import ManyAdapter
from kivyfn.store.adapter.single import Adapter
from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events.event import (
    InsertEvent,
    SetEvent,
    SetRowEvent,
    StoreEvent,
)
from kivyfn.store.remote.store import RemoteStore
from kivyfn.store.remote.transport import (
    EventBatchHandler,
    LifecycleEvent,
    LifecycleHandler,
    TransportHandler,
)


SyncHook = Callable[[], Awaitable[None]]


class FakeTransport:
    def __init__(self) -> None:
        self._connected = False
        self._event_handler: EventBatchHandler | None = None
        self._signal_handler: TransportHandler | None = None
        self._lifecycle_handler: LifecycleHandler | None = None
        self.instance_id = uuid4().hex
        self.data: dict[tuple[str, ...], Any] = {}
        self.revisions: dict[tuple[str, ...], int] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.signal_bindings: dict[str, dict[str, Any]] = {}
        self._token_counter = 0
        self._subscribe_calls = 0
        self._signal_bind_calls = 0
        self.fail_subscribe_at: int | None = None
        self.fail_signal_bind_at: int | None = None
        self.sync_hook: SyncHook | None = None

    def set_event_handler(
        self,
        handler: EventBatchHandler,
    ) -> None:
        self._event_handler = handler

    def set_signal_handler(
        self,
        handler: TransportHandler,
    ) -> None:
        self._signal_handler = handler

    def set_lifecycle_handler(
        self,
        handler: LifecycleHandler,
    ) -> None:
        self._lifecycle_handler = handler

    async def connect(self) -> None:
        if self._connected:
            return

        self._connected = True
        self._notify_lifecycle("connected")

    async def disconnect(self) -> None:
        if not self._connected:
            return

        self._connected = False
        self.subscriptions.clear()
        self.signal_bindings.clear()
        self._notify_lifecycle("disconnected")

    async def drop(self) -> None:
        await self.disconnect()

    async def reconnect(self) -> None:
        await self.connect()

    async def call(
        self,
        event: str,
        message: Any | None = None,
    ) -> Any:
        if not self._connected:
            raise RuntimeError("transport is disconnected")

        payload = message if isinstance(message, dict) else {}

        if event == "subscribe":
            return self._subscribe(payload)

        if event == "unsubscribe":
            token = payload["subscription_token"]
            self.subscriptions.pop(token, None)
            return self._success(True)

        if event == "signal_bind":
            return self._bind_signal(payload)

        if event == "signal_unbind":
            token = payload["binding_token"]
            self.signal_bindings.pop(token, None)
            return self._success(True)

        if event == "sync_snapshot":
            return await self._sync_snapshot(payload)

        if event == "revision_audit":
            return self._revision_audit(payload)

        if event == "snapshot":
            key = tuple(payload["address"])
            return self._success(deepcopy(self.data[key]))

        if event == "signal_emit":
            return self._success(True)

        raise AssertionError(f"Unsupported fake call: {event}")

    def seed(
        self,
        address: Address[Any],
        value: Any,
        *,
        revision: int = 0,
    ) -> None:
        key = address.parts
        self.data[key] = deepcopy(value)
        self.revisions[key] = revision

    async def setrow(
        self,
        address: Address[Any],
        id: str,
        key: str,
        value: Any,
        *,
        deliver: bool = True,
        revision: int | None = None,
    ) -> SetRowEvent:
        address_key = address.parts
        row = self.data[address_key][id]
        old_value = row.get(key)
        row[key] = value
        next_revision = (
            revision
            if revision is not None
            else self.revisions[address_key] + 1
        )
        self.revisions[address_key] = next_revision
        event = SetRowEvent(
            address=address,
            id=id,
            key=key,
            old_value=old_value,
            new_value=value,
            server_instance_id=self.instance_id,
            revision=next_revision,
        )

        if deliver:
            await self._deliver(event)

        return event

    async def set(
        self,
        address: Address[Any],
        key: str,
        value: Any,
        *,
        deliver: bool = True,
        revision: int | None = None,
    ) -> SetEvent:
        address_key = address.parts
        old_value = self.data[address_key].get(key)
        self.data[address_key][key] = value
        next_revision = (
            revision
            if revision is not None
            else self.revisions[address_key] + 1
        )
        self.revisions[address_key] = next_revision
        event = SetEvent(
            address=address,
            key=key,
            old_value=old_value,
            new_value=value,
            server_instance_id=self.instance_id,
            revision=next_revision,
        )

        if deliver:
            await self._deliver(event)

        return event

    async def insert(
        self,
        address: Address[Any],
        id: str,
        value: dict[str, Any],
        *,
        deliver: bool = True,
        revision: int | None = None,
    ) -> InsertEvent[dict[str, Any]]:
        address_key = address.parts
        self.data[address_key][id] = deepcopy(value)
        next_revision = (
            revision
            if revision is not None
            else self.revisions[address_key] + 1
        )
        self.revisions[address_key] = next_revision
        event = InsertEvent(
            address=address,
            id=id,
            new_value=deepcopy(value),
            server_instance_id=self.instance_id,
            revision=next_revision,
        )

        if deliver:
            await self._deliver(event)

        return event

    async def deliver(self, event: StoreEvent) -> None:
        await self._deliver(event)

    def _subscribe(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._subscribe_calls += 1

        if self.fail_subscribe_at == self._subscribe_calls:
            raise RuntimeError("injected subscribe failure")

        token = self._new_token("sub")
        event_token = self._new_token("event")
        self.subscriptions[token] = {
            "event_token": event_token,
            "spec": deepcopy(payload),
        }
        return self._success(
            {
                "subscription_token": token,
                "event_token": event_token,
            }
        )

    def _bind_signal(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._signal_bind_calls += 1

        if self.fail_signal_bind_at == self._signal_bind_calls:
            raise RuntimeError("injected signal-bind failure")

        token = self._new_token("binding")
        event_token = self._new_token("signal")
        self.signal_bindings[token] = {
            "event_token": event_token,
            "spec": deepcopy(payload),
        }
        return self._success(
            {
                "binding_token": token,
                "event_token": event_token,
            }
        )

    async def _sync_snapshot(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        key = tuple(payload["address"])
        data = deepcopy(self.data[key])
        revision = self.revisions[key]
        hook = self.sync_hook
        self.sync_hook = None

        if hook is not None:
            await hook()

        return self._success(
            {
                "server_instance_id": self.instance_id,
                "revision": revision,
                "data": data,
            }
        )

    def _revision_audit(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._success(
            {
                "server_instance_id": self.instance_id,
                "revisions": [
                    {
                        "address": parts,
                        "revision": self.revisions[tuple(parts)],
                    }
                    for parts in payload["addresses"]
                ],
            }
        )

    async def _deliver(self, event: StoreEvent) -> None:
        handler = self._event_handler

        if handler is None or not self._connected:
            return

        messages: list[dict[str, Any]] = []

        for registration in self.subscriptions.values():
            spec = registration["spec"]

            if tuple(spec["address"]) != event.address.parts:
                continue

            if spec["opcode"] != event.opcode.value:
                continue

            messages.append(
                {
                    "event_token": registration["event_token"],
                    "event": event.payload(),
                }
            )

        if messages:
            await handler(messages)

    def _notify_lifecycle(self, event: LifecycleEvent) -> None:
        handler = self._lifecycle_handler

        if handler is not None:
            handler(event)

    def _new_token(self, prefix: str) -> str:
        self._token_counter += 1
        return f"{prefix}-{self._token_counter}"

    @staticmethod
    def _success(data: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "data": data,
            "error": None,
        }

    @property
    def connected(self) -> bool:
        return self._connected


async def wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(
        poll(),
        timeout=timeout,
    )


def create_store(
    transport: FakeTransport,
    *,
    audit_interval: float = 60.0,
) -> RemoteStore:
    return RemoteStore(
        "test",
        transport,
        audit_interval=audit_interval,
        recovery_delay=0.001,
    )


@pytest.mark.asyncio
async def test_initial_snapshot_race_replays_buffered_event() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)

    async def race_update() -> None:
        await transport.setrow(
            address,
            "stage",
            "time",
            "18:00:00",
        )

    transport.sync_hook = race_update
    await adapter.load()

    assert adapter.data["stage"]["time"] == "18:00:00"
    assert adapter.revision == 1
    assert adapter.synchronized
    await store.disconnect()


@pytest.mark.asyncio
async def test_reconnect_rebinds_and_repairs_offline_change() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    repaired: list[SetRowEvent] = []
    adapter.bind(
        on_setrow=lambda _adapter, event: repaired.append(event),
    )
    await adapter.load()
    old_tokens = {
        subscription.token
        for subscription in adapter._subscriptions
    }

    await transport.drop()
    await transport.setrow(
        address,
        "stage",
        "time",
        "18:00:00",
        deliver=False,
    )
    await transport.reconnect()
    await asyncio.wait_for(
        store.client.wait_until_ready(),
        timeout=1.0,
    )

    new_tokens = {
        subscription.token
        for subscription in adapter._subscriptions
    }
    assert new_tokens.isdisjoint(old_tokens)
    assert adapter.data["stage"]["time"] == "18:00:00"
    assert repaired[-1].new_value == "18:00:00"
    assert adapter.synchronized
    await store.disconnect()


@pytest.mark.asyncio
async def test_revision_gap_triggers_targeted_resynchronization() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    await adapter.load()

    event = await transport.setrow(
        address,
        "stage",
        "time",
        "18:00:00",
        deliver=False,
        revision=2,
    )
    await transport.deliver(event)
    await wait_for(
        lambda: (
            adapter.synchronized
            and adapter.revision == 2
        )
    )

    assert adapter.data["stage"]["time"] == "18:00:00"
    await store.disconnect()


@pytest.mark.asyncio
async def test_callback_inconsistency_recovers_from_snapshot() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(address, {})
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    await adapter.load()

    transport.data[address.parts]["stage"] = {
        "time": "18:00:00",
    }
    transport.revisions[address.parts] = 1
    event = SetRowEvent(
        address=address,
        id="stage",
        key="time",
        old_value="20:00:00",
        new_value="18:00:00",
        server_instance_id=transport.instance_id,
        revision=1,
    )
    await transport.deliver(event)
    await wait_for(
        lambda: (
            adapter.synchronized
            and "stage" in adapter.data
        )
    )

    assert adapter.data["stage"]["time"] == "18:00:00"
    await store.disconnect()


@pytest.mark.asyncio
async def test_revision_audit_repairs_silent_mismatch() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(
        transport,
        audit_interval=0.01,
    )
    await store.connect()
    adapter = ManyAdapter(store, address)
    await adapter.load()
    await transport.setrow(
        address,
        "stage",
        "time",
        "18:00:00",
        deliver=False,
    )
    await wait_for(
        lambda: adapter.data["stage"]["time"] == "18:00:00",
    )

    assert adapter.revision == 1
    await store.disconnect()


@pytest.mark.asyncio
async def test_partial_adapter_load_rolls_back_and_retries() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(address, {})
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    transport.fail_subscribe_at = 2

    with pytest.raises(RuntimeError, match="injected subscribe failure"):
        await adapter.load()

    assert not transport.subscriptions
    assert not adapter._subscriptions
    assert not adapter.loaded

    transport.fail_subscribe_at = None
    await adapter.load()

    assert adapter.loaded
    assert adapter.synchronized
    assert len(transport.subscriptions) == 3
    await store.disconnect()


@pytest.mark.asyncio
async def test_signal_binding_is_recreated_after_reconnect() -> None:
    address = Address("lighting")
    transport = FakeTransport()
    transport.seed(address, {})
    store = create_store(transport)
    await store.connect()
    response = await store.signal.bind(
        address,
        "changed",
        lambda _event: None,
    )
    assert response.ok is True
    binding = response.data
    assert binding is not None
    old_token = binding.token

    await transport.drop()
    await transport.reconnect()
    await asyncio.wait_for(
        store.client.wait_until_ready(),
        timeout=1.0,
    )

    assert binding.token != old_token
    assert binding.token in transport.signal_bindings
    await store.disconnect()


@pytest.mark.asyncio
async def test_explicit_disconnect_and_reconnect_restores_adapter() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    await adapter.load()

    await store.disconnect()
    assert adapter.data["stage"]["time"] == "20:00:00"
    assert not adapter.synchronized

    await transport.setrow(
        address,
        "stage",
        "time",
        "18:00:00",
        deliver=False,
    )
    await store.connect()

    assert adapter.data["stage"]["time"] == "18:00:00"
    assert adapter.synchronized
    assert len(transport.subscriptions) == 3
    await store.disconnect()


@pytest.mark.asyncio
async def test_server_instance_change_triggers_audit_repair() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(
        transport,
        audit_interval=0.01,
    )
    await store.connect()
    adapter = ManyAdapter(store, address)
    await adapter.load()
    old_instance_id = adapter.server_instance_id

    transport.instance_id = uuid4().hex
    transport.data[address.parts]["stage"]["time"] = "18:00:00"
    transport.revisions[address.parts] = 0
    await wait_for(
        lambda: (
            adapter.server_instance_id == transport.instance_id
            and adapter.data["stage"]["time"] == "18:00:00"
        )
    )

    assert adapter.server_instance_id != old_instance_id
    assert adapter.synchronized
    await store.disconnect()


@pytest.mark.asyncio
async def test_multiple_adapters_recover_together() -> None:
    stage_address = Address("lighting_stage")
    program_address = Address("lighting_program")
    transport = FakeTransport()
    transport.seed(
        stage_address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    transport.seed(
        program_address,
        {
            "program": {
                "enabled": True,
            }
        },
    )
    store = create_store(transport)
    await store.connect()
    stage_adapter = ManyAdapter(store, stage_address)
    program_adapter = ManyAdapter(store, program_address)
    await asyncio.gather(
        stage_adapter.load(),
        program_adapter.load(),
    )

    await transport.drop()
    await transport.setrow(
        stage_address,
        "stage",
        "time",
        "18:00:00",
        deliver=False,
    )
    await transport.setrow(
        program_address,
        "program",
        "enabled",
        False,
        deliver=False,
    )
    await transport.reconnect()
    await asyncio.wait_for(
        store.client.wait_until_ready(),
        timeout=1.0,
    )

    assert stage_adapter.data["stage"]["time"] == "18:00:00"
    assert not program_adapter.data["program"]["enabled"]
    assert stage_adapter.synchronized
    assert program_adapter.synchronized
    await store.disconnect()


@pytest.mark.asyncio
async def test_single_adapter_reconnects_and_reconciles() -> None:
    address = Address("lighting_settings")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "enabled": True,
            "minimum": 10,
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = Adapter(store, address)
    repaired: list[SetEvent] = []
    adapter.bind(
        on_set=lambda _adapter, event: repaired.append(event),
    )
    await adapter.load()

    await transport.drop()
    await transport.set(
        address,
        "minimum",
        20,
        deliver=False,
    )
    await transport.reconnect()
    await asyncio.wait_for(
        store.client.wait_until_ready(),
        timeout=1.0,
    )

    assert adapter.data["minimum"] == 20
    assert repaired[-1].new_value == 20
    assert adapter.synchronized
    await store.disconnect()


@pytest.mark.asyncio
async def test_cached_reads_continue_but_mutations_are_blocked() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    await adapter.load()

    await transport.drop()

    assert adapter.data["stage"]["time"] == "20:00:00"

    with pytest.raises(StoreStateError, match="not synchronized"):
        await adapter.setrow(
            "stage",
            "time",
            "18:00:00",
        )

    await store.disconnect()


@pytest.mark.asyncio
async def test_reconnect_subscription_staging_rolls_back_atomically() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(address, {})
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    await adapter.load()
    transport.fail_subscribe_at = transport._subscribe_calls + 2

    with pytest.raises(RuntimeError, match="injected subscribe failure"):
        await store.client._synchronize()

    assert not transport.subscriptions

    transport.fail_subscribe_at = None
    await store.connect()
    assert adapter.synchronized
    assert len(transport.subscriptions) == 3
    await store.disconnect()


@pytest.mark.asyncio
async def test_reconnect_signal_staging_rolls_back_atomically() -> None:
    address = Address("lighting")
    transport = FakeTransport()
    transport.seed(address, {})
    store = create_store(transport)
    await store.connect()
    first = await store.signal.bind(
        address,
        "first",
        lambda _event: None,
    )
    second = await store.signal.bind(
        address,
        "second",
        lambda _event: None,
    )
    assert first.ok
    assert second.ok
    transport.fail_signal_bind_at = transport._signal_bind_calls + 2

    with pytest.raises(RuntimeError, match="injected signal-bind failure"):
        await store.client._synchronize()

    assert not transport.signal_bindings

    transport.fail_signal_bind_at = None
    await store.connect()
    assert len(transport.signal_bindings) == 2
    await store.disconnect()


@pytest.mark.asyncio
async def test_reconciliation_events_have_deterministic_order() -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "remove-first": {
                "value": 1,
            },
            "keep": {
                "value": 1,
            },
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    changes: list[tuple[str, str, str | None]] = []
    adapter.bind(
        on_remove=lambda _adapter, event: changes.append(
            ("remove", event.id, None)
        ),
        on_insert=lambda _adapter, event: changes.append(
            ("insert", event.id, None)
        ),
        on_setrow=lambda _adapter, event: changes.append(
            ("setrow", event.id, event.key)
        ),
    )
    await adapter.load()

    transport.data[address.parts] = {
        "keep": {
            "value": 2,
        },
        "insert-last": {
            "value": 3,
        },
    }
    transport.revisions[address.parts] = 3
    await adapter.resync("test-reconciliation")

    assert changes == [
        ("remove", "remove-first", None),
        ("insert", "insert-last", None),
        ("setrow", "keep", "value"),
    ]
    await store.disconnect()


@pytest.mark.asyncio
async def test_failed_consumer_callback_is_replayed_by_reconciliation(
) -> None:
    address = Address("lighting_stage")
    transport = FakeTransport()
    transport.seed(
        address,
        {
            "stage": {
                "time": "20:00:00",
            }
        },
    )
    store = create_store(transport)
    await store.connect()
    adapter = ManyAdapter(store, address)
    callback_values: list[str] = []
    fail_once = True

    def consume(_adapter: object, event: SetRowEvent) -> None:
        nonlocal fail_once
        callback_values.append(event.new_value)

        if fail_once:
            fail_once = False
            raise RuntimeError("injected consumer failure")

    adapter.bind(
        on_setrow=consume,
    )
    await adapter.load()
    await transport.setrow(
        address,
        "stage",
        "time",
        "18:00:00",
    )
    await wait_for(
        lambda: (
            adapter.synchronized
            and adapter.data["stage"]["time"] == "18:00:00"
        )
    )

    assert callback_values == ["18:00:00", "18:00:00"]
    await store.disconnect()
