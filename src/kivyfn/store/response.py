# src/kivyfn/store/response.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeAlias, TypeVar, cast

from kivyfn.store.error import StoreFailure, StoreProtocolError
from kivyfn.store.error.codec import decode_failure, encode_failure

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SuccessResponse(Generic[T]):
    """Successful Store wire response."""

    data: T

    ok: Literal[True] = field(default=True, init=False)

    error: None = field(default=None, init=False)

    def payload(self) -> dict[str, Any]:
        return {"ok": True, "data": self.data}


@dataclass(frozen=True, slots=True)
class FailureResponse:
    """Expected Store-domain rejection transported over the wire."""

    error: StoreFailure

    ok: Literal[False] = field(default=False, init=False)

    data: None = field(default=None, init=False)

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": encode_failure(self.error)}


Response: TypeAlias = SuccessResponse[T] | FailureResponse


def success(data: T) -> SuccessResponse[T]:
    return SuccessResponse(data=data)


def failure(error: StoreFailure) -> FailureResponse:
    return FailureResponse(error=error)


def from_payload(value: Any) -> Response[Any]:
    """
    Decode one Store response envelope.

    Envelope shape is intentionally strict:

        success:
            {
                "ok": True,
                "data": ...
            }

        failure:
            {
                "ok": False,
                "error": {...}
            }

    StoreFailure decoding remains delegated to the error codec.
    """
    if not isinstance(value, Mapping):
        raise StoreProtocolError("Store response must be an object")

    payload = dict(value)

    ok = payload.get("ok")

    if not isinstance(ok, bool):
        raise StoreProtocolError(
            "Store response is missing a boolean 'ok' field"
        )

    if ok:
        _validate_keys(payload, required={"ok", "data"})

        return SuccessResponse(data=payload["data"])

    _validate_keys(payload, required={"ok", "error"})

    error_payload = payload["error"]

    if not isinstance(error_payload, Mapping):
        raise StoreProtocolError(
            "Failed Store response error must be an object"
        )

    try:
        decoded = decode_failure(dict(error_payload))

    except Exception as exc:
        raise StoreProtocolError(
            ("Failed Store response contains an invalid error payload")
        ) from exc

    if not isinstance(decoded, StoreFailure):
        raise StoreProtocolError(
            ("Failed Store response error codec did not return StoreFailure")
        )

    return FailureResponse(error=cast(StoreFailure, decoded))


def _validate_keys(payload: Mapping[str, Any], *, required: set[str]) -> None:
    keys = set(payload)

    missing = required - keys

    if missing:
        names = ", ".join(sorted(missing))

        raise StoreProtocolError(
            (f"Store response is missing required fields: {names}")
        )

    unexpected = keys - required

    if unexpected:
        names = ", ".join(sorted(unexpected))

        raise StoreProtocolError(
            (f"Store response contains unexpected fields: {names}")
        )
