from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeAlias, TypeVar

T_co = TypeVar("T_co", covariant=True)
T = TypeVar("T")

ErrorPayload: TypeAlias = dict[str, Any]


@dataclass(slots=True, frozen=True)
class SuccessResponse(Generic[T_co]):
    ok: Literal[True]
    status: int
    data: T_co
    error: None = None

    def payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "data": self.data,
            "error": self.error,
        }


@dataclass(slots=True, frozen=True)
class FailureResponse:
    ok: Literal[False]
    status: int
    error: ErrorPayload
    data: None = None

    def payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "data": self.data,
            "error": self.error,
        }


Response: TypeAlias = SuccessResponse[T_co] | FailureResponse


def success(
    data: T,
    *,
    status: int = 200,
) -> SuccessResponse[T]:
    return SuccessResponse(
        ok=True,
        status=status,
        data=data,
    )


def failure(
    *,
    status: int,
    error: ErrorPayload,
) -> FailureResponse:
    return FailureResponse(
        ok=False,
        status=status,
        error=error,
    )


def from_payload(
    payload: dict[str, Any],
) -> Response[Any]:
    if payload["ok"] is True:
        return SuccessResponse(
            ok=True,
            status=payload["status"],
            data=payload["data"],
        )

    error = payload.get("error")

    if not isinstance(error, dict):
        error = {
            "type": "ProtocolError",
            "code": "missing_error_payload",
            "message": "Failure response contained no valid error payload",
        }

    return FailureResponse(
        ok=False,
        status=payload["status"],
        error=error,
    )


