from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

import pytest

from kivyfn.store.error import (
    FieldErrorDetail,
    StoreErrorCategory,
    StoreErrorCode,
    StoreErrorStatus,
    StoreFailure,
    StoreProtocolError,
)
from kivyfn.store.response import (
    FailureResponse,
    SuccessResponse,
    failure,
    from_payload,
    success,
)


@pytest.mark.parametrize("data", [None, 0, False, "", [], {"nested": [1]}])
def test_legacy_success_envelope_preserves_valid_falsy_values(
    data: Any,
) -> None:
    original = success(data)
    assert isinstance(original, SuccessResponse)
    assert original.ok is True
    assert original.error is None
    decoded = from_payload(json.loads(json.dumps(original.payload())))
    assert isinstance(decoded, SuccessResponse)
    assert decoded.data == data


def test_legacy_failure_envelope_preserves_structured_domain_failure() -> None:
    problem = StoreFailure(
        code=StoreErrorCode.FIELD_VALIDATION_FAILED,
        category=StoreErrorCategory.FIELD,
        status=StoreErrorStatus.INVALID,
        message="Value exceeds maximum",
        detail=FieldErrorDetail(field="age", value=200, maximum=130),
    )
    original = failure(problem)
    assert original.ok is False
    assert original.data is None
    decoded = from_payload(json.loads(json.dumps(original.payload())))
    assert isinstance(decoded, FailureResponse)
    assert decoded.error == problem


def test_legacy_decoder_accepts_general_mapping_envelopes() -> None:
    decoded = from_payload(MappingProxyType({"ok": True, "data": 3}))
    assert isinstance(decoded, SuccessResponse)
    assert decoded.data == 3


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"ok": 1, "data": None},
        {"ok": "true", "data": None},
        {"ok": True},
        {"ok": True, "data": None, "error": None},
        {"ok": False},
        {"ok": False, "error": None},
        {"ok": False, "error": []},
        {"ok": False, "error": {}},
        {"ok": False, "error": {}, "data": None},
    ],
)
def test_legacy_decoder_rejects_malformed_envelopes(payload: Any) -> None:
    with pytest.raises(StoreProtocolError):
        from_payload(payload)
