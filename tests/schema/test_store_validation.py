from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kivyfn.store import Store
from kivyfn.store.builder import Branch, Builder
from kivyfn.store.error import FieldValidationError
from kivyfn.store.protocol.message.requests import SetRequestAction
from kivyfn.store.schema import (
    DatetimeField,
    NumericField,
    SingleSchema,
    TimeField,
)


@pytest.mark.parametrize(
    ("field", "invalid", "valid"),
    [
        (TimeField(default="12:00Z", ge="11:00Z"), "12:00", "13:00Z"),
        (TimeField(default="12:00", le="14:00"), "12:00Z", "13:00"),
        (
            DatetimeField(default="2026-01-01T12:00Z", ge="2026-01-01T11:00Z"),
            "2026-01-01T12:00",
            "2026-01-01T13:00Z",
        ),
        (
            DatetimeField(default="2026-01-01T12:00", le="2026-01-01T14:00"),
            "2026-01-01T12:00Z",
            "2026-01-01T13:00",
        ),
        (NumericField(default=1, ge=0, le=10), float("nan"), 2),
        (NumericField(default=1), float("inf"), 2),
    ],
)
def test_expected_field_failure_does_not_mutate_or_shut_down_store(
    tmp_path: Path, field: Any, invalid: Any, valid: Any
) -> None:
    schema_type = type("ValidationSchema", (SingleSchema,), {"value": field})
    config = Builder().tree(Branch(schema_type)).storage_path(tmp_path).build()
    store = Store(config)
    store.boot()
    commits: list[Any] = []
    store.subscribe_commits(commits.append)
    try:
        result = store.execute(
            (SetRequestAction("ValidationSchema", (("value", invalid),)),),
            transaction_id="invalid",
        )
        assert not result.success
        assert isinstance(result.error, FieldValidationError)
        assert store.snapshot_by_schema("ValidationSchema") == {
            "value": field.default
        }
        assert store.get_node_by_schema("ValidationSchema").revision == 0
        assert commits == []
        result = store.execute(
            (SetRequestAction("ValidationSchema", (("value", valid),)),),
            transaction_id="valid",
        )
        assert result.success
        assert store.snapshot_by_schema("ValidationSchema") == {"value": valid}
        assert store.get_node_by_schema("ValidationSchema").revision == 1
        assert len(commits) == 1
    finally:
        store.shutdown()
