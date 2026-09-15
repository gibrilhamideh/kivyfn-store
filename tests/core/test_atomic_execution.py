from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kivyfn.store.error import (
    FieldNotFoundError,
    FieldValidationError,
    RowAlreadyExistsError,
    RowNotFoundError,
    RowOperationConflictError,
    UniqueConstraintError,
)
from kivyfn.store.events import StoreCommit
from kivyfn.store.node import Node
from kivyfn.store.protocol.message.requests import RemoveRequestAction
from kivyfn.store.schema import (
    DataField,
    ManySchema,
    NumericField,
    SingleSchema,
    StringField,
)
from tests.core.helpers import insert, make_store, setrow, setvalue


class Users(ManySchema):
    email = StringField(default="")
    number = NumericField(default=0)
    payload = DataField[dict[str, Any]](default={"items": []})
    uniques = [("email",)]
    indexes = [("number",)]


class Settings(SingleSchema):
    number = NumericField(default=0)
    payload = DataField[dict[str, Any]](default={"items": []})


def test_unique_swap_commits_final_rows_and_indexes(tmp_path: Path) -> None:
    store = make_store(tmp_path, Users, Settings)
    assert store.execute(
        (insert(Users, "a", email="a"), insert(Users, "b", email="b")),
        transaction_id="seed",
    ).success
    observations = []
    store.subscribe_commits(
        lambda commit: observations.append(
            (commit, store.snapshot_by_schema("Users"))
        )
    )
    result = store.execute(
        (
            setvalue(Settings, number=99),
            setrow(Users, "a", email="b"),
            setrow(Users, "b", email="a"),
        ),
        transaction_id="swap",
    )
    assert result.success
    assert result.commit is not None
    assert result.commit.schema_revisions == {"Users": 2, "Settings": 1}
    users = store.get_node_by_schema("Users")
    assert users.lookup_one("email", "b") == "a"
    assert users.lookup_one("email", "a") == "b"
    assert len(observations) == 1
    assert observations[0][1]["b"]["email"] == "a"


def test_invalid_final_unique_leaves_all_nodes_and_revisions_unchanged(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Users, Settings)
    store.execute(
        (insert(Users, "a", email="a"), insert(Users, "b", email="b")),
        transaction_id="seed",
    )
    commits: list[StoreCommit] = []
    store.subscribe_commits(commits.append)
    result = store.execute(
        (setvalue(Settings, number=99), setrow(Users, "a", email="b")),
        transaction_id="duplicate",
    )
    assert isinstance(result.error, UniqueConstraintError)
    assert store.snapshot_by_schema("Settings", key="number") == 0
    assert store.snapshot_by_schema("Users", id="a", key="email") == "a"
    assert store.get_node_by_schema("Users").lookup_one("email", "a") == "a"
    assert store.schema_revisions["Users"] == 1
    assert store.schema_revisions["Settings"] == 0
    assert not store.is_shutdown
    assert commits == []


def test_unexpected_prepare_failure_does_not_publish_partial_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path, Users, Settings)
    users = store.get_node_by_schema("Users")
    settings = store.get_node_by_schema("Settings")
    prepare = Node.prepare

    def fail_second(self: Node, data: dict[str, Any]):
        if self is users:
            raise RuntimeError("deterministic index preparation failure")
        return prepare(self, data)

    monkeypatch.setattr(Node, "prepare", fail_second)
    with pytest.raises(RuntimeError, match="index preparation"):
        store.execute(
            (setvalue(Settings, number=99), insert(Users, "a", email="a")),
            transaction_id="fail",
        )
    assert settings.snapshot() == {"number": 0, "payload": {"items": []}}
    assert users.snapshot() == {}
    assert users.revision == settings.revision == 0


@pytest.mark.parametrize(
    ("actions", "error"),
    [
        ((setrow(Users, "missing", number=1),), RowNotFoundError),
        ((RemoveRequestAction("Users", "missing"),), RowNotFoundError),
        ((insert(Users, "a"),), RowAlreadyExistsError),
        (
            (RemoveRequestAction("Users", "a"), setrow(Users, "a", number=1)),
            RowOperationConflictError,
        ),
        ((RemoveRequestAction("Users", "a"),) * 2, RowOperationConflictError),
        ((insert(Users, "b", unknown=1),), FieldNotFoundError),
        ((setrow(Users, "a", number="invalid"),), FieldValidationError),
    ],
)
def test_rejected_sequences_never_commit_prefix(
    tmp_path: Path, actions: tuple, error: type[Exception]
) -> None:
    store = make_store(tmp_path, Users, Settings)
    store.execute((insert(Users, "a", email="a"),), transaction_id="seed")
    before = store.snapshot_by_schema("Users")
    result = store.execute(
        (setvalue(Settings, number=42), *actions), transaction_id="invalid"
    )
    assert isinstance(result.error, error)
    assert store.snapshot_by_schema("Users") == before
    assert store.snapshot_by_schema("Settings", key="number") == 0
    assert not store.is_shutdown


def test_repeated_values_and_membership_replacement_event_history(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Users, Settings)
    result = store.execute(
        (
            insert(Users, "a", email="a"),
            setrow(Users, "a", number=1),
            setrow(Users, "a", number=1),
            RemoveRequestAction("Users", "a"),
            insert(Users, "a", email="b", number=2),
            setrow(Users, "a", number=3),
            setvalue(Settings, number=1),
            setvalue(Settings, number=0),
        ),
        transaction_id="sequence",
    )
    assert result.success
    assert result.commit is not None
    assert len(result.commit.events) == 7
    assert store.snapshot_by_schema("Users", id="a", key="number") == 3
    assert store.snapshot_by_schema("Settings", key="number") == 0
    node = store.get_node_by_schema("Users")
    assert node.lookup_one("email", "a") is None
    assert node.lookup_one("email", "b") == "a"
    assert node.lookup_all("number", 1) == frozenset()
    assert node.lookup_all("number", 3) == frozenset({"a"})
    assert result.commit.schema_revisions == {"Users": 1, "Settings": 1}


def test_insert_remove_cancels_final_membership_but_preserves_events(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Users)
    result = store.execute(
        (insert(Users, "a"), RemoveRequestAction("Users", "a")),
        transaction_id="cancel",
    )
    assert result.success
    assert result.commit is not None
    assert len(result.commit.events) == 2
    assert store.snapshot_by_schema("Users") == {}


def test_noop_and_empty_transactions_do_not_publish_or_advance(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, Settings)
    commits = []
    store.subscribe_commits(commits.append)
    assert store.execute((), transaction_id="empty").success
    assert store.execute(
        (setvalue(Settings, number=0),), transaction_id="noop"
    ).success
    assert commits == []
    assert store.schema_revisions["Settings"] == 0


def test_composite_unique_swap_and_nullable_keys(tmp_path: Path) -> None:
    class Pairs(ManySchema):
        first = StringField(default=None, allow_none=True)
        second = NumericField(default=0)
        uniques = [("first", "second")]

    store = make_store(tmp_path, Pairs)
    assert store.execute(
        (
            insert(Pairs, "a", first="same", second=1),
            insert(Pairs, "b", first="same", second=2),
            insert(Pairs, "c"),
            insert(Pairs, "d"),
        ),
        transaction_id="seed",
    ).success
    assert store.execute(
        (setrow(Pairs, "a", second=2), setrow(Pairs, "b", second=1)),
        transaction_id="swap",
    ).success
    node = store.get_node_by_schema("Pairs")
    assert node.lookup_one(("first", "second"), ("same", 1)) == "b"
    assert node.lookup_one(("first", "second"), (None, 0)) is None


def test_unhashable_indexed_value_is_domain_failure(tmp_path: Path) -> None:
    class Indexed(ManySchema):
        value = DataField[Any](default="ok")
        indexes = [("value",)]

    store = make_store(tmp_path, Indexed, Settings)
    result = store.execute(
        (setvalue(Settings, number=5), insert(Indexed, "a", value=[1])),
        transaction_id="unhashable",
    )
    assert isinstance(result.error, FieldValidationError)
    assert store.snapshot_by_schema("Settings", key="number") == 0
    assert store.snapshot_by_schema("Indexed") == {}
    assert not store.is_shutdown
