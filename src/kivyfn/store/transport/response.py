from __future__ import annotations
from typing import Any, Generic, TypeVar, Self

from dataclasses import dataclass, replace


T = TypeVar("T")


@dataclass(slots=True, frozen=True)
class Response(Generic[T]):
    ok: bool
    status: int
    data: T | Any = None
    error: dict[str, Any] | None = None

    def __repr__(self) -> str:
        if self.ok:
            return f"Response<OK status={self.status}>"

        err = self.error or {}
        return (
            f"Response<ERROR\n"
            f"  status={self.status}\n"
            f"  type={err.get('type')}\n"
            f"  code={err.get('code')}\n"
            f"  message={err.get('message')}\n"
            f">"
        )

    def payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "data": self.data,
            "error": self.error,
        }

    def copy_with(self, **changes: Any) -> Self:
        return replace(self, **changes)

    @classmethod
    def success(
        cls,
        data: T | None = None,
        *,
        status: int = 200,
    ) -> Response[T]:
        return cls(
            ok=True,
            status=status,
            data=data,
            error=None,
        )

    @classmethod
    def failure(
        cls,
        *,
        status: int,
        error: dict[str, Any],
    ) -> Response[Any]:
        return cls(
            ok=False,
            status=status,
            data=None,
            error=error,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Response[Any]:
        return cls(
            ok=payload["ok"],
            status=payload["status"],
            data=payload.get("data"),
            error=payload.get("error"),
        )
