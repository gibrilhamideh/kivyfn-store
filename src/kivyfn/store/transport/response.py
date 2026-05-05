from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar, Self


# Scenario	status	code
# success	200	"ok" / "snapshot"
# validation error	422	"validation_failed"
# bad input	400	"invalid_payload"
# not found	404	"not_found"
# internal error	500	"internal_error"

T = TypeVar("T")


@dataclass(slots=True, frozen=True)
class Response(Generic[T]):
    ok: bool
    status: int
    message: str = ""
    data: T | Any = None

    def payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }

    def copy_with(self, **changes: Any) -> Self:
        return replace(self, **changes)

    @classmethod
    def success(
        cls,
        data: T | None = None,
        *,
        status: int = 200,
        message: str = "",
    ) -> Response[T]:
        return cls(ok=True, status=status, message=message, data=data)

    @classmethod
    def error(
        cls,
        message: str,
        *,
        status: int = 400,
        data: T | None = None,
    ) -> Response[T]:
        return cls(ok=False, status=status, message=message, data=data)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Response[Any]:
        return cls(
            ok=payload["ok"],
            status=payload["status"],
            message=payload.get("message", ""),
            data=payload.get("data"),
        )
