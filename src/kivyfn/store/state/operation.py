from __future__ import annotations

import asyncio

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from kivyfn.logging import get_logger
from kivyfn.store.utils.tools.uuid import generate_uuid


if TYPE_CHECKING:
    from kivyfn.store.state.base import StateEvents


logger = get_logger(__name__)


class StateOperationKind(str, Enum):
    UPDATE = "update"
    INSERT = "insert"
    REMOVE = "remove"
    SIGNAL = "signal"
    TRANSACTION = "transaction"


@dataclass(frozen=True, slots=True)
class StateResult:
    ok: bool
    changed: bool
    data: Any = None
    error: Any = None


class StateOperation:
    """One state request that starts immediately and may be awaited."""

    def __init__(
        self,
        kind: StateOperationKind,
        *,
        schema: str | None = None,
        row_id: str | None = None,
        signal: str | None = None,
        requested: Any = None,
        previous: Any = None,
        optimistic: bool = False,
    ) -> None:
        self.id = generate_uuid()
        self.kind = kind
        self.schema = schema
        self.row_id = row_id
        self.signal = signal
        self.requested = requested
        self.previous = previous
        self.optimistic = optimistic

        self._states: set[StateEvents] = set()
        self._waiters: list[
            tuple[
                asyncio.AbstractEventLoop,
                asyncio.Future[StateResult],
            ]
        ] = []
        self._result: StateResult | None = None

    def __await__(self):
        return self.wait().__await__()

    async def wait(self) -> StateResult:
        result = self._result

        if result is not None:
            return result

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[StateResult] = loop.create_future()
        self._waiters.append((loop, waiter))
        return await waiter

    def _attach(self, state: StateEvents) -> None:
        if self._result is not None or state in self._states:
            return

        self._states.add(state)
        state._begin_operation(self)

    def _finish(self, result: StateResult) -> None:
        if self._result is not None:
            return

        self._result = result
        waiters = tuple(self._waiters)
        states = tuple(self._states)
        self._waiters.clear()
        self._states.clear()

        for loop, waiter in waiters:
            loop.call_soon_threadsafe(
                _set_waiter_result,
                waiter,
                result,
            )

        for state in states:
            try:
                state._finish_operation(self, result)
            except Exception:
                logger.exception(
                    "State operation callback failed: id=%s kind=%s",
                    self.id,
                    self.kind.value,
                )

    @property
    def done(self) -> bool:
        return self._result is not None

    @property
    def result(self) -> StateResult | None:
        return self._result

    @property
    def ok(self) -> bool | None:
        result = self._result
        return result.ok if result is not None else None

    @property
    def changed(self) -> bool | None:
        result = self._result
        return result.changed if result is not None else None

    @property
    def data(self) -> Any:
        result = self._result
        return result.data if result is not None else None

    @property
    def error(self) -> Any:
        result = self._result
        return result.error if result is not None else None

    @property
    def fields(self) -> tuple[str, ...]:
        requested = self.requested

        if not isinstance(requested, dict):
            return ()

        return tuple(requested)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"id={self.id!r}, "
            f"kind={self.kind.value!r}, "
            f"schema={self.schema!r}, "
            f"row_id={self.row_id!r}, "
            f"done={self.done!r}"
            ")"
        )


def _set_waiter_result(
    waiter: asyncio.Future[StateResult],
    result: StateResult,
) -> None:
    if not waiter.done():
        waiter.set_result(result)
