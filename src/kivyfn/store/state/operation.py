# src/kivyfn/store/state/operation.py
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Self, TypeAlias

from kivy.event import EventDispatcher

from kivyfn.logging import get_logger
from kivyfn.store.utils.tools.uuid import generate_uuid

if TYPE_CHECKING:
    from kivyfn.store.state.failure import StateFailure


logger = get_logger(__name__)


StateOperationKind: TypeAlias = Literal[
    "set",
    "insert",
    "replace",
    "remove",
    "signal",
    "transaction",
]


class StateOperation(EventDispatcher):
    """One State mutation request and its terminal lifecycle."""

    __events__ = (
        "on_success",
        "on_failed",
        "on_done",
    )

    def __init__(
        self,
        kind: StateOperationKind,
        *,
        optimistic: bool = False,
    ) -> None:
        super().__init__()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "StateOperation requires a running asyncio loop"
            ) from exc

        self.id = generate_uuid()
        self.kind = kind
        self.optimistic = optimistic

        self._loop = loop
        self._done = False
        self._failure: StateFailure | None = None
        self._completion = loop.create_future()

    def __await__(self):
        return self.wait().__await__()

    async def wait(self) -> Self:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError(
                "StateOperation must be awaited on its owning event loop"
            )

        await asyncio.shield(self._completion)

        return self

    def _succeed(self) -> None:
        self._finish(None)

    def _fail(
        self,
        failure: StateFailure,
    ) -> None:
        self._finish(failure)

    def _finish(
        self,
        failure: StateFailure | None,
    ) -> None:
        if self._done:
            return

        self._done = True
        self._failure = failure

        self._loop.call_soon_threadsafe(self._dispatch_completion)

    def _dispatch_completion(self) -> None:
        failure = self._failure

        try:
            if failure is None:
                self.dispatch("on_success")
            else:
                self.dispatch("on_failed", failure)
        except Exception:
            logger.exception(
                "State operation completion callback failed: "
                "id=%s kind=%s",
                self.id,
                self.kind,
            )

        try:
            self.dispatch("on_done", failure)
        except Exception:
            logger.exception(
                "State operation done callback failed: "
                "id=%s kind=%s",
                self.id,
                self.kind,
            )

        if not self._completion.done():
            self._completion.set_result(None)

    @property
    def done(self) -> bool:
        return self._done

    @property
    def success(self) -> bool:
        return self._done and self._failure is None

    @property
    def failed(self) -> bool:
        return self._done and self._failure is not None

    @property
    def failure(self) -> StateFailure | None:
        return self._failure

    def on_success(self) -> None:
        pass

    def on_failed(
        self,
        failure: StateFailure,
    ) -> None:
        pass

    def on_done(
        self,
        failure: StateFailure | None,
    ) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"id={self.id!r}, "
            f"kind={self.kind!r}, "
            f"optimistic={self.optimistic!r}, "
            f"done={self.done!r}"
            ")"
        )
