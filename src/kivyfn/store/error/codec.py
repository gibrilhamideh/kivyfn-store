# src/kivyfn/store/error/codec.py
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, TypeVar, cast

from kivyfn.store.error.category import StoreErrorCategory
from kivyfn.store.error.code import StoreErrorCode
from kivyfn.store.error.context import StoreErrorContext
from kivyfn.store.error.detail import (
    AddressErrorDetail,
    ConstraintErrorDetail,
    FieldErrorDetail,
    IndexErrorDetail,
    InstructionErrorDetail,
    NodeErrorDetail,
    ProtocolValue,
    RowErrorDetail,
    RuntimeErrorDetail,
    StoreErrorDetail,
)
from kivyfn.store.error.failure import StoreFailure
from kivyfn.store.error.stage import StoreErrorStage
from kivyfn.store.error.status import StoreErrorStatus


DataclassT = TypeVar("DataclassT")

EnumT = TypeVar("EnumT", bound=Enum)


_DETAIL_TYPES: dict[str, type[StoreErrorDetail]] = {
    "instruction": InstructionErrorDetail,
    "address": AddressErrorDetail,
    "node": NodeErrorDetail,
    "field": FieldErrorDetail,
    "row": RowErrorDetail,
    "index": IndexErrorDetail,
    "constraint": ConstraintErrorDetail,
    "runtime": RuntimeErrorDetail,
}


_DETAIL_NAMES: dict[type[StoreErrorDetail], str] = {
    detail_type: name for name, detail_type in _DETAIL_TYPES.items()
}


def encode_failure(failure: StoreFailure) -> dict[str, ProtocolValue]:
    """
    Encode a StoreFailure into a transport-safe protocol payload.
    """
    payload: dict[str, ProtocolValue] = {
        "code": failure.code.value,
        "category": failure.category.value,
        "status": failure.status.value,
        "message": failure.message,
        "detail": None,
        "context": None,
    }

    if failure.detail is not None:
        payload["detail"] = _encode_detail(failure.detail)

    if failure.context is not None:
        payload["context"] = _encode_context(failure.context)

    return payload


def decode_failure(payload: object) -> StoreFailure:
    """
    Decode and validate a transport payload into a StoreFailure.

    Raises ValueError when the payload does not conform to the store error
    protocol.
    """
    if not isinstance(payload, dict):
        raise ValueError("Store failure payload must be a dictionary")

    code = _decode_enum(StoreErrorCode, payload.get("code"), field="code")

    category = _decode_enum(
        StoreErrorCategory, payload.get("category"), field="category"
    )

    status = _decode_enum(
        StoreErrorStatus, payload.get("status"), field="status"
    )

    message = payload.get("message")

    if not isinstance(message, str) or not message:
        raise ValueError("Store failure message must be a non-empty string")

    detail_payload = payload.get("detail")
    context_payload = payload.get("context")

    detail = None if detail_payload is None else _decode_detail(detail_payload)

    context = (
        None if context_payload is None else _decode_context(context_payload)
    )

    return StoreFailure(
        code=code,
        category=category,
        status=status,
        message=message,
        detail=detail,
        context=context,
    )


def _encode_detail(detail: StoreErrorDetail) -> dict[str, ProtocolValue]:
    detail_name = _DETAIL_NAMES.get(type(detail))

    if detail_name is None:
        raise TypeError(
            f"Unsupported store error detail type: {type(detail).__name__}"
        )

    payload = _encode_dataclass(detail)

    payload["type"] = detail_name

    return payload


def _decode_detail(payload: Any) -> StoreErrorDetail:
    if not isinstance(payload, dict):
        raise ValueError("Store failure detail must be a dictionary")

    detail_name = payload.get("type")

    if not isinstance(detail_name, str):
        raise ValueError("Store failure detail requires a string type")

    detail_type = _DETAIL_TYPES.get(detail_name)

    if detail_type is None:
        raise ValueError(f"Unknown store failure detail type {detail_name!r}")

    values = {key: value for key, value in payload.items() if key != "type"}

    return _construct_dataclass(detail_type, values)


def _encode_context(context: StoreErrorContext) -> dict[str, ProtocolValue]:
    return _encode_dataclass(context)


def _decode_context(payload: Any) -> StoreErrorContext:
    if not isinstance(payload, dict):
        raise ValueError("Store failure context must be a dictionary")

    values = dict(payload)

    stage = values.get("stage")

    if stage is not None:
        values["stage"] = _decode_enum(
            StoreErrorStage, stage, field="context.stage"
        )

    return _construct_dataclass(StoreErrorContext, values)


def _encode_dataclass(value: object) -> dict[str, ProtocolValue]:
    """
    Encode a dataclass instance into protocol-safe values.

    The Any cast is intentional because the standard-library dataclass typing
    does not narrow instances correctly for Pyright.
    """
    if isinstance(value, type) or not is_dataclass(value):
        raise TypeError(f"{type(value).__name__} is not a dataclass instance")

    raw = asdict(cast(Any, value))

    normalized = _normalize_value(raw)

    if not isinstance(normalized, dict):
        raise TypeError("Encoded dataclass must produce a dictionary")

    return normalized


def _construct_dataclass(
    dataclass_type: type[DataclassT], values: dict[str, Any]
) -> DataclassT:
    """
    Validate fields and construct a dataclass instance.
    """
    if not is_dataclass(dataclass_type):
        raise TypeError(f"{dataclass_type.__name__} is not a dataclass type")

    dataclass_fields = getattr(dataclass_type, "__dataclass_fields__", None)

    if not isinstance(dataclass_fields, dict):
        raise TypeError(
            f"{dataclass_type.__name__} has invalid dataclass metadata"
        )

    allowed_fields = set(dataclass_fields)
    unknown_fields = set(values) - allowed_fields

    if unknown_fields:
        names = ", ".join(sorted(unknown_fields))

        raise ValueError(
            f"Unknown fields for {dataclass_type.__name__}: {names}"
        )

    normalized = dict(values)

    address = normalized.get("address")

    if address is not None:
        normalized["address"] = _decode_string_tuple(
            address, field=f"{dataclass_type.__name__}.address"
        )

    fields_value = normalized.get("fields")

    if fields_value is not None:
        normalized["fields"] = _decode_string_tuple(
            fields_value, field=f"{dataclass_type.__name__}.fields"
        )

    referencing_row_ids = normalized.get("referencing_row_ids")

    if referencing_row_ids is not None:
        normalized["referencing_row_ids"] = _decode_string_tuple(
            referencing_row_ids,
            field=(f"{dataclass_type.__name__}.referencing_row_ids"),
        )

    try:
        constructor = cast(Any, dataclass_type)

        instance = constructor(**normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {dataclass_type.__name__} payload") from exc

    return cast(DataclassT, instance)


def _decode_string_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")

    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must contain only strings")

    return tuple(value)


def _normalize_value(value: Any) -> ProtocolValue:
    if value is None:
        return None

    if isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, Enum):
        return _normalize_value(value.value)

    if isinstance(value, dict):
        normalized: dict[str, ProtocolValue] = {}

        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "Store protocol dictionaries require string keys"
                )

            normalized[key] = _normalize_value(item)

        return normalized

    if isinstance(value, (list, tuple, set)):
        return [_normalize_value(item) for item in value]

    raise TypeError(
        f"Value of type {type(value).__name__} "
        "is not supported by the store protocol"
    )


def _decode_enum(enum_type: type[EnumT], value: Any, *, field: str) -> EnumT:
    if not isinstance(value, str):
        raise ValueError(f"Store failure {field} must be a string")

    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"Unknown store failure {field} {value!r}") from exc
