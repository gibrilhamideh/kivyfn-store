from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from kivyfn.store import HostConfig, Store, StoreClient, StoreHost
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.error import InvalidOperationError
from kivyfn.store.state.manager import StateManager
from kivyfn.store.storage import FileStorage
from tests.boot.test_boot import Rows, legacy
from tests.conftest import TransportKind, _client_endpoint, _host_endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["raise", "recover", "erase"])
@pytest.mark.parametrize("source", ["legacy", "generation"])
async def test_invalid_saved_identity_obeys_policy_and_can_subscribe(
    tmp_path: Path,
    transport_kind: TransportKind,
    policy: Literal["raise", "recover", "erase"],
    source: str,
) -> None:
    data = {
        "": {"label": "invalid", "count": 1},
        "good": {"label": "retained", "count": 3},
    }
    if source == "legacy":
        legacy(tmp_path, "Rows", data)
    else:
        FileStorage(tmp_path).write_store({"Rows": data}, store_version=1)
    config = (
        Builder()
        .tree(Branch(Rows))
        .storage_path(tmp_path)
        .boot_policy(policy)
        .build()
    )
    store = Store(config)
    if policy == "raise":
        with pytest.raises(InvalidOperationError, match="non-empty string"):
            store.boot()
        saved = FileStorage(tmp_path).read("Rows")
        assert saved is not None and saved["data"] == data
        return

    store.boot()
    expected = {"good": data["good"]}
    assert store.snapshot_by_schema("Rows") == expected
    saved = FileStorage(tmp_path).read("Rows")
    assert saved is not None and saved["data"] == expected

    host = StoreHost(store, HostConfig(_host_endpoint(transport_kind)))
    client = StoreClient()
    manager = StateManager(client)
    rows = manager.create(Rows)
    try:
        await host.start()
        await client.connect(_client_endpoint(host))
        assert rows.synced
        assert set(rows) == {"good"}
        assert rows["good"].count == 3
        transaction = rows["good"].set(count=4)
        await transaction
        assert transaction.success
        assert rows["good"].count == 4
        assert store.snapshot_by_schema("Rows", id="good", key="count") == 4
    finally:
        manager.close()
        await client.disconnect()
        await host.stop()
        store.shutdown()
