# tests/store/test_transaction.py
from __future__ import annotations

from typing import Any

import pytest

from kivyfn.logging import get_logger
from kivyfn.store.core.execution import ExecutionResult
from kivyfn.store.error import InvalidOperationError, StoreErrorStage
from kivyfn.store.events import SetEvent, StoreCommit
from kivyfn.store.operations import OperationSource
from kivyfn.store.protocol.message.requests import SetRequestAction
from kivyfn.store.store import store as store_module
from kivyfn.store.store.store import Store


logger = get_logger(__name__)


class FakeNode:
    def __init__(self) -> None:
        self.revision = 0

    def advance_revision(self) -> int:
        self.revision += 1
        return self.revision


class FakeMetadata:
    def __init__(self) -> None:
        self.nodes_by_schema = {
            "DemoSchema": FakeNode(),
        }


class FakeInstructionBuilder:
    def __init__(self, metadata: FakeMetadata) -> None:
        self.metadata = metadata

    def build(
        self,
        actions: tuple[SetRequestAction, ...],
    ) -> tuple[SetRequestAction, ...]:
        return actions


class FakeEngine:
    result: ExecutionResult

    def __init__(self, root: Any, config: Any) -> None:
        self.metadata = FakeMetadata()
        self.booted = False
        self.transaction_id: str | None = None

    def boot(self) -> None:
        self.booted = True

    def execute(
        self,
        instructions: tuple[SetRequestAction, ...],
        *,
        transaction_id: str,
    ) -> ExecutionResult:
        self.transaction_id = transaction_id
        return self.result

    def get_node_by_schema(self, schema: str) -> FakeNode:
        return self.metadata.nodes_by_schema[schema]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setattr(store_module, "Engine", FakeEngine)
    monkeypatch.setattr(
        store_module,
        "InstructionBuilder",
        FakeInstructionBuilder,
    )

    instance = Store(root=object(), config=object())
    instance.boot()

    return instance


def test_successful_transaction_publishes_commit(store: Store) -> None:
    transaction_id = "transaction-success"
    event = SetEvent(
        schema="DemoSchema",
        source=OperationSource.COMMAND,
        key="value",
        old=0,
        new=10,
    )

    FakeEngine.result = ExecutionResult(
        success=True,
        stage=StoreErrorStage.EXECUTE,
        commit=StoreCommit(events=(event,), transaction_id=transaction_id),
    )

    commits: list[StoreCommit] = []
    store.subscribe_commits(commits.append)

    result = store.execute(
        (
            SetRequestAction(
                schema="DemoSchema",
                values=(("value", 10),),
            ),
        ),
        transaction_id=transaction_id,
    )

    assert result.success
    assert result.commit is not None
    assert result.commit.transaction_id == transaction_id
    assert result.commit.schema_revisions == {"DemoSchema": 1}
    assert commits == [result.commit]


def test_schema_revision_advances_once_per_transaction(store: Store) -> None:
    events = (
        SetEvent(
            schema="DemoSchema",
            source=OperationSource.COMMAND,
            key="first",
            old=0,
            new=1,
        ),
        SetEvent(
            schema="DemoSchema",
            source=OperationSource.COMMAND,
            key="second",
            old=0,
            new=2,
        ),
    )

    FakeEngine.result = ExecutionResult(
        success=True,
        stage=StoreErrorStage.EXECUTE,
        commit=StoreCommit(
            events=events,
            transaction_id="transaction-revision",
        ),
    )

    result = store.execute(
        (
            SetRequestAction(
                schema="DemoSchema",
                values=(("first", 1), ("second", 2)),
            ),
        ),
        transaction_id="transaction-revision",
    )

    assert result.success
    assert result.commit is not None
    assert result.commit.schema_revisions == {"DemoSchema": 1}
    assert store.schema_revisions == {"DemoSchema": 1}


def test_failed_transaction_does_not_publish_commit(store: Store) -> None:
    error = InvalidOperationError("Transaction rejected")

    FakeEngine.result = ExecutionResult(
        success=False,
        stage=StoreErrorStage.VALIDATE,
        error=error,
    )

    commits: list[StoreCommit] = []
    store.subscribe_commits(commits.append)

    result = store.execute(
        (
            SetRequestAction(
                schema="DemoSchema",
                values=(("value", 10),),
            ),
        ),
        transaction_id="transaction-failure",
    )

    assert not result.success
    assert result.error is error
    assert result.commit is None
    assert commits == []
    assert store.schema_revisions == {"DemoSchema": 0}


def test_unsubscribed_commit_listener_is_not_called(store: Store) -> None:
    event = SetEvent(
        schema="DemoSchema",
        source=OperationSource.COMMAND,
        key="value",
        old=0,
        new=10,
    )

    FakeEngine.result = ExecutionResult(
        success=True,
        stage=StoreErrorStage.EXECUTE,
        commit=StoreCommit(
            events=(event,),
            transaction_id="transaction-unsubscribe",
        ),
    )

    commits: list[StoreCommit] = []
    unsubscribe = store.subscribe_commits(commits.append)
    unsubscribe()

    store.execute(
        (
            SetRequestAction(
                schema="DemoSchema",
                values=(("value", 10),),
            ),
        ),
        transaction_id="transaction-unsubscribe",
    )

    assert commits == []
