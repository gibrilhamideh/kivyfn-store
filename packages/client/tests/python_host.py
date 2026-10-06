"""Real Python v3 host used by the TypeScript interoperability tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from kivyfn.store.builder import (
    Branch,
    Builder,
    HistoryConfig,
    HostConfig,
    RelayConfig,
)
from kivyfn.store.host.host import StoreHost
from kivyfn.store.schema import (
    ForeignKeyField,
    ManySchema,
    NumericField,
    Signal,
    SingleSchema,
    StringField,
)
from kivyfn.store.store import Store


class ClientRoot(SingleSchema):
    persistent = True
    track_history = True
    value = NumericField(default=0, ge=0, le=100)
    label = StringField(default="root")
    ping = Signal(undo="unping")
    unping = Signal(undo="ping")


class ClientRow(ManySchema):
    persistent = True
    track_history = True
    label = StringField(default="")
    value = NumericField(default=0)
    parent_id = ForeignKeyField("ClientRow")
    ping = Signal()


async def start(path: Path, gateway: str) -> tuple[Store, StoreHost]:
    relay = (
        RelayConfig(
            endpoint=gateway,
            store_id="unit-1",
            auth={"token": "test-store"},
            reconnect_interval=0.05,
            request_timeout=2,
        )
        if gateway
        else None
    )
    config = (
        Builder()
        .tree(Branch(ClientRoot).child("rows", Branch(ClientRow)))
        .storage_path(path)
        .history(HistoryConfig())
        .host(
            HostConfig(
                "http://127.0.0.1:0/store/socket.io",
                devtools=False,
                relay=relay,
            )
        )
        .build()
    )
    store = Store(config)
    store.boot()
    host = StoreHost(store)
    await host.start()
    if host.relay is not None:
        await host.relay.wait_online(timeout=5)
    return store, host


def ready(host: StoreHost) -> None:
    print(
        json.dumps(
            {"kind": "ready", "endpoint": host.transport.endpoint["endpoint"]}
        ),
        flush=True,
    )


async def main() -> None:
    path = Path(sys.argv[1])
    gateway = sys.argv[2] if len(sys.argv) > 2 else ""
    store, host = await start(path, gateway)
    try:
        ready(host)
        while command := await asyncio.to_thread(sys.stdin.readline):
            command = command.strip()
            if command == "stop":
                break
            if command == "restart":
                await host.stop()
                store.shutdown()
                store, host = await start(path, gateway)
                ready(host)
            elif command == "relay-reconnect":
                assert host.relay is not None
                await host.relay.stop()
                await host.relay.start(host)
                await host.relay.wait_online(timeout=5)
                ready(host)
    finally:
        await host.stop()
        store.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
