from __future__ import annotations

import json
from typing import Any

import pytest

from kivyfn.store.error import (
    AddressErrorDetail,
    ConstraintErrorDetail,
    FieldErrorDetail,
    FieldValidationError,
    IndexErrorDetail,
    InstructionErrorDetail,
    NodeErrorDetail,
    RowErrorDetail,
    RuntimeErrorDetail,
    StoreErrorCategory,
    StoreErrorCode,
    StoreErrorContext,
    StoreErrorDetail,
    StoreErrorStage,
    StoreErrorStatus,
    StoreFailure,
    decode_failure,
    encode_failure,
)


def failure_payload() -> dict[str, Any]:
    return encode_failure(
        StoreFailure(
            code=StoreErrorCode.FIELD_VALIDATION_FAILED,
            category=StoreErrorCategory.FIELD,
            status=StoreErrorStatus.INVALID,
            message="Invalid field",
        )
    )


@pytest.mark.parametrize(
    "detail",
    [
        None,
        AddressErrorDetail(address=(), segment="missing"),
        InstructionErrorDetail(
            opcode="setrow",
            address=("rows",),
            row_id="r",
            field="age",
            value={"bad": [None, 1, True]},
            source="command",
        ),
        NodeErrorDetail(address=("a", "b"), schema="Rows"),
        FieldErrorDetail(
            schema="Rows",
            field="age",
            value=-1,
            rule="ge",
            minimum=0,
            maximum=10.0,
            allow_none=False,
            expected=[1, 2, 3],
        ),
        RowErrorDetail(
            schema="Rows",
            address=("rows",),
            row_id="r",
            operation="remove",
            conflicting_operation="insert",
        ),
        IndexErrorDetail(schema="Rows", fields=("a", "b"), value=[1, "b"]),
        ConstraintErrorDetail(
            schema="Rows",
            fields=("a",),
            value="taken",
            row_id="r",
            existing_row_id="existing",
            reference_schema="Parent",
            reference_field="id",
            referencing_schema="Children",
            referencing_field="parent_id",
            referencing_row_ids=("c1", "c2"),
        ),
        RuntimeErrorDetail(store="main", operation="connect", transport="io"),
    ],
)
def test_all_structured_details_roundtrip_through_json(
    detail: StoreErrorDetail | None,
) -> None:
    failure = StoreFailure(
        code=StoreErrorCode.FIELD_VALIDATION_FAILED,
        category=StoreErrorCategory.FIELD,
        status=StoreErrorStatus.INVALID,
        message="Invalid field",
        detail=detail,
        context=StoreErrorContext(
            stage=StoreErrorStage.VALIDATE,
            schema="Rows",
            address=(),
            opcode="setrow",
            source="command",
            row_id="r",
            field="age",
            value={"nested": [1, None, "text", False]},
            instruction_index=0,
        ),
    )
    payload = json.loads(json.dumps(encode_failure(failure), allow_nan=False))
    decoded = decode_failure(payload)
    assert decoded == failure
    assert decoded is not failure
    assert decoded.context is not failure.context


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("code", "unknown"),
        ("code", 1),
        ("category", "unknown"),
        ("category", []),
        ("status", "unknown"),
        ("status", True),
        ("message", ""),
        ("message", 1),
        ("message", None),
        ("detail", "bad"),
        ("context", []),
        ("unexpected", 1),
    ],
)
def test_failure_rejects_invalid_top_level_fields(
    name: str, value: Any
) -> None:
    payload = failure_payload()
    payload[name] = value
    with pytest.raises(ValueError):
        decode_failure(payload)


@pytest.mark.parametrize("payload", [None, [], "failure", 1, {}])
def test_failure_rejects_missing_or_nonmapping_payload(payload: Any) -> None:
    with pytest.raises(ValueError):
        decode_failure(payload)


@pytest.mark.parametrize(
    "detail",
    [
        {},
        {"type": 1},
        {"type": "unknown"},
        {"type": "field", "field": 42},
        {"type": "field", "schema": {}},
        {"type": "field", "rule": []},
        {"type": "field", "minimum": "bad"},
        {"type": "field", "maximum": True},
        {"type": "field", "maximum": float("inf")},
        {"type": "field", "allow_none": {}},
        {"type": "field", "allow_none": 1},
        {"type": "field", "expected": {1: "bad"}},
        {"type": "field", "value": {"nested": [object()]}},
        {"type": "field", "value": float("nan")},
        {"type": "field", "value": (1,)},
        {"type": "field", "other": 1},
        {"type": "field", 42: 1},
        {"type": "address"},
        {"type": "address", "address": None},
        {"type": "address", "address": "path"},
        {"type": "address", "address": [1]},
        {"type": "index", "fields": None},
        {"type": "index", "fields": [1]},
        {"type": "constraint", "referencing_row_ids": None},
        {"type": "constraint", "referencing_row_ids": [True]},
        {"type": "constraint", "existing_row_id": []},
        {"type": "instruction", "opcode": 1},
        {"type": "node", "schema": 1},
        {"type": "row", "conflicting_operation": 1},
        {"type": "runtime", "transport": 1},
    ],
)
def test_failure_rejects_malformed_typed_detail(detail: Any) -> None:
    payload = failure_payload()
    payload["detail"] = detail
    with pytest.raises(ValueError):
        decode_failure(payload)


@pytest.mark.parametrize(
    "context",
    [
        {"stage": "unknown"},
        {"stage": 1},
        {"schema": 1},
        {"address": "path"},
        {"opcode": []},
        {"source": 1},
        {"row_id": []},
        {"field": 1},
        {"value": object()},
        {"instruction_index": "1"},
        {"instruction_index": True},
        {"instruction_index": -1},
        {"instruction_index": 1.0},
        {"unknown": 1},
        {1: "bad"},
    ],
)
def test_failure_rejects_malformed_typed_context(context: Any) -> None:
    payload = failure_payload()
    payload["context"] = context
    with pytest.raises(ValueError):
        decode_failure(payload)


def test_failure_value_ownership_is_detached_at_wire_boundary() -> None:
    payload = failure_payload()
    payload["detail"] = {"type": "field", "value": {"nested": [1]}}
    failure = decode_failure(payload)
    payload["detail"]["value"]["nested"].append(2)
    encoded: Any = encode_failure(failure)
    assert encoded["detail"]["value"] == {"nested": [1]}
    encoded["detail"]["value"]["nested"].append(3)
    assert getattr(failure.detail, "value") == {"nested": [1]}


def test_nonfinite_rejected_value_can_be_reported_as_json_diagnostic() -> None:
    failure = FieldValidationError(
        "Invalid float", detail=FieldErrorDetail(value=float("nan"))
    ).failure()
    encoded: Any = encode_failure(failure)
    json.dumps(encoded, allow_nan=False)
    assert encoded["detail"]["value"] == "nan"


def test_unknown_detail_classes_fail_instead_of_silently_losing_data() -> None:
    failure = StoreFailure(
        code=StoreErrorCode.FIELD_VALIDATION_FAILED,
        category=StoreErrorCategory.FIELD,
        status=StoreErrorStatus.INVALID,
        message="Invalid field",
        detail=StoreErrorDetail(),
    )
    with pytest.raises(TypeError, match="Unsupported"):
        encode_failure(failure)


def test_expected_errors_keep_type_when_context_is_added() -> None:
    error = FieldValidationError("Invalid", detail=FieldErrorDetail(field="a"))
    context = StoreErrorContext(stage=StoreErrorStage.COMPILE)
    assert error.with_context(context) is error
    failure = error.failure()
    assert failure.context == context
    assert failure.detail == error.detail
    assert failure.code is StoreErrorCode.FIELD_VALIDATION_FAILED
