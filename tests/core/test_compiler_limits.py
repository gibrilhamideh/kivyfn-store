from __future__ import annotations

from pathlib import Path

import pytest

from kivyfn.store.core.compiler import Compiler
from kivyfn.store.error import InvalidOperationError
from kivyfn.store.protocol.message.requests import RemoveRequestAction
from kivyfn.store.schema import NumericField, SingleSchema
from tests.core.helpers import insert, make_store, setvalue
from tests.core.test_relationships_and_triggers import Children, Parents


@pytest.mark.parametrize("limit", [1, 2])
def test_cascade_limit_counts_actual_removals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    store = make_store(tmp_path, Parents, Children)
    try:
        assert store.execute(
            (
                insert(Parents, "parent"),
                insert(Children, "child", parent_id="parent"),
            ),
            transaction_id="seed",
        ).success
        before = store.checkpoint()
        commits = []
        store.subscribe_commits(commits.append)
        monkeypatch.setattr(Compiler, "MAX_OPERATIONS", limit)
        result = store.execute(
            (
                RemoveRequestAction("Parents", "parent"),
                RemoveRequestAction("Children", "child"),
            ),
            transaction_id="cascade",
        )
        if limit == 2:
            assert result.success and result.commit is not None
            assert len(result.commit.events) == 2
            assert store.snapshot_by_schema("Parents") == {}
            assert store.snapshot_by_schema("Children") == {}
            assert len(commits) == 1
        else:
            assert isinstance(result.error, InvalidOperationError)
            assert store.checkpoint() == before
            assert commits == []
    finally:
        store.shutdown()


def test_noop_after_exact_operation_limit_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Root(SingleSchema):
        value = NumericField(default=0)

    store = make_store(tmp_path, root_schema=Root)
    try:
        monkeypatch.setattr(Compiler, "MAX_OPERATIONS", 1)
        result = store.execute(
            (setvalue(Root, value=1), setvalue(Root, value=1)),
            transaction_id="one-effect",
        )
        assert result.success and result.commit is not None
        assert len(result.commit.events) == 1
        assert store.snapshot_by_schema("Root", key="value") == 1
        before = store.checkpoint()
        rejected = store.execute(
            (setvalue(Root, value=2), setvalue(Root, value=3)),
            transaction_id="two-effects",
        )
        assert isinstance(rejected.error, InvalidOperationError)
        assert store.checkpoint() == before
    finally:
        store.shutdown()
