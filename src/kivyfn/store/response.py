# kivyfn/store/response.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeAlias, TypeVar

from kivyfn.store.error.codec import (
    decode_failure,
    encode_failure,
)
from kivyfn.store.error.detail import ProtocolValue
from kivyfn.store.error.failure import StoreFailure
from kivyfn.store.error.runtime import StoreProtocolError


T_co = TypeVar(
    "T_co",
    covariant=True,
)

T = TypeVar("T")


@dataclass(
    frozen=True,
    slots=True,
)
class SuccessResponse(Generic[T_co]):
    """
    Successful store operation response.
    """

    ok: Literal[True]
    data: T_co
    error: None = None

    def payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "data": self.data,
            "error": None,
        }


@dataclass(
    frozen=True,
    slots=True,
)
class FailureResponse:
    """
    Expected store operation rejection.

    The failure contains the same normalized StoreFailure object for both
    LocalStore and RemoteStore.
    """

    ok: Literal[False]
    error: StoreFailure
    data: None = None

    def payload(self) -> dict[str, ProtocolValue]:
        return {
            "ok": False,
            "data": None,
            "error": encode_failure(
                self.error,
            ),
        }


Response: TypeAlias = (
    SuccessResponse[T_co]
    | FailureResponse
)


def success(
    data: T,
) -> SuccessResponse[T]:
    """
    Create a successful store response.
    """
    return SuccessResponse(
        ok=True,
        data=data,
    )


def failure(
    error: StoreFailure,
) -> FailureResponse:
    """
    Create a failed store response from a normalized StoreFailure.
    """
    return FailureResponse(
        ok=False,
        error=error,
    )


def from_payload(
    payload: dict[str, Any],
) -> Response[Any]:
    """
    Decode a serialized store response.

    Raises StoreProtocolError when the payload violates the response protocol.
    """
    if not isinstance(payload, dict):
        raise StoreProtocolError(
            "Store response payload must be a dictionary"
        )

    ok = payload.get("ok")

    if ok is True:
        if "data" not in payload:
            raise StoreProtocolError(
                "Successful store response is missing data"
            )

        return SuccessResponse(
            ok=True,
            data=payload["data"],
        )

    if ok is False:
        error_payload = payload.get("error")

        if not isinstance(error_payload, dict):
            raise StoreProtocolError(
                "Failed store response is missing a valid error payload"
            )

        try:
            error = decode_failure(
                error_payload,
            )
        except (TypeError, ValueError) as exc:
            raise StoreProtocolError(
                "Failed store response contains an invalid error payload"
            ) from exc

        return FailureResponse(
            ok=False,
            error=error,
        )

    raise StoreProtocolError(
        "Store response contains an invalid ok value"
    )


