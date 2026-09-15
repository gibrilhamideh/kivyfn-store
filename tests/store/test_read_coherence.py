from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, RLock, get_ident
from typing import Any

import pytest

from kivyfn.store.address import Address
from kivyfn.store.error import StoreStateError
from kivyfn.store.events import StoreCommit
from kivyfn.store.query import Query
from kivyfn.store.schema import ManySchema, NumericField, SingleSchema
from tests.core.helpers import insert, make_store, setrow, setvalue


class Root(SingleSchema):
    number = NumericField(default=0)


class Rows(ManySchema):
    number = NumericField(default=0)


class ObservedLock:
    """Signal a reader's lock attempt without relying on scheduler delays."""

    def __init__(self) -> None:
        self.lock = RLock()
        self.reader: int | None = None
        self.attempted = Event()
        self.progress = Event()

    def __enter__(self) -> ObservedLock:
        if get_ident() == self.reader:
            self.attempted.set()
            self.progress.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self.lock.release()


@pytest.mark.parametrize(
    "api",
    [
        "query",
        "execute_query",
        "snapshot_if_changed",
        "snapshot_by_schema",
        "snapshot_by_address",
        "schema_revisions",
    ],
)
def test_public_read_waits_for_values_and_revisions_to_commit_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str
) -> None:
    store = make_store(tmp_path, Rows, root_schema=Root)
    store.execute((insert(Rows, "a"),), transaction_id="seed")
    query = Query(Rows)
    plan = store.compile_query(query)
    readers = {
        "query": lambda: store.query(query),
        "execute_query": lambda: store.execute_query(plan),
        "snapshot_if_changed": lambda: store.snapshot_if_changed("Rows"),
        "snapshot_by_schema": lambda: store.snapshot_by_schema("Rows"),
        "snapshot_by_address": lambda: store.snapshot_by_address(
            Address("Rows")
        ),
        "schema_revisions": lambda: store.schema_revisions,
    }
    entered = Event()
    allow_stamp = Event()
    finished = Event()
    lock = ObservedLock()
    record = store._record_commit
    monkeypatch.setattr(store, "_lock", lock)

    def paused_record(commit: StoreCommit) -> StoreCommit:
        # Engine has installed the values and released its own lock. Store's
        # lock must still prevent readers observing the unstamped generation.
        entered.set()
        assert allow_stamp.wait(timeout=5)
        return record(commit)

    monkeypatch.setattr(store, "_record_commit", paused_record)

    def read() -> Any:
        lock.reader = get_ident()
        try:
            return readers[api]()
        finally:
            finished.set()
            lock.progress.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            store.execute,
            (setrow(Rows, "a", number=1), setvalue(Root, number=1)),
            transaction_id="change",
        )
        assert entered.wait(timeout=5)
        reader = executor.submit(read)
        try:
            assert lock.progress.wait(timeout=5)
            assert lock.attempted.is_set(), "Read bypassed the Store lock"
            assert not finished.is_set(), "Read returned before revision stamp"
        finally:
            allow_stamp.set()
        assert writer.result(timeout=5).success
        result = reader.result(timeout=5)

    if api in {"query", "execute_query"}:
        assert result[0]["number"] == 1
    elif api == "snapshot_if_changed":
        assert result == (2, {"a": {"number": 1}})
    elif api == "schema_revisions":
        assert result == {"Root": 1, "Rows": 2}
    else:
        assert result == {"a": {"number": 1}}


@pytest.mark.parametrize(
    "api",
    [
        "query",
        "execute_query",
        "compile_query",
        "snapshot_if_changed",
        "snapshot_by_schema",
        "snapshot_by_address",
        "schema_revisions",
        "get_node_by_schema",
        "get_node_by_address",
        "subscribe_commits",
    ],
)
def test_waiting_read_rechecks_lifecycle_after_acquiring_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str
) -> None:
    store = make_store(tmp_path, Rows, root_schema=Root)
    query = Query(Rows)
    plan = store.compile_query(query)
    readers = {
        "query": lambda: store.query(query),
        "execute_query": lambda: store.execute_query(plan),
        "compile_query": lambda: store.compile_query(query),
        "snapshot_if_changed": lambda: store.snapshot_if_changed("Rows"),
        "snapshot_by_schema": lambda: store.snapshot_by_schema("Rows"),
        "snapshot_by_address": lambda: store.snapshot_by_address(Address()),
        "schema_revisions": lambda: store.schema_revisions,
        "get_node_by_schema": lambda: store.get_node_by_schema("Rows"),
        "get_node_by_address": lambda: store.get_node_by_address(Address()),
        "subscribe_commits": lambda: store.subscribe_commits(lambda _: None),
    }
    lock = ObservedLock()
    monkeypatch.setattr(store, "_lock", lock)

    def read() -> Any:
        lock.reader = get_ident()
        try:
            return readers[api]()
        finally:
            lock.progress.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with lock:
            reader = executor.submit(read)
            assert lock.progress.wait(timeout=5)
            assert lock.attempted.is_set(), "Read bypassed the Store lock"
            store.shutdown()
        with pytest.raises(StoreStateError, match="shut down"):
            reader.result(timeout=5)
