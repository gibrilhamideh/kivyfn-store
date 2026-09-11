# src/kivyfn/store/state/mutation/operation.py
from __future__ import annotations

import asyncio

from typing import Literal

from uuid import uuid4

from kivy.event import EventDispatcher
from kivy.properties import BooleanProperty, ObjectProperty, StringProperty

from kivyfn.store.state.mutation.failure import StateFailure


StateOperationKind = Literal[
    "set", "insert", "replace", "remove", "signal", "transaction"
]


class StateOperation(EventDispatcher):
    """Awaitable lifecycle handle for one State mutation."""

    done = BooleanProperty(False)
    success = BooleanProperty(False)
    failed = BooleanProperty(False)

    failure = ObjectProperty(None, allownone=True)

    id = StringProperty()
    kind = StringProperty()
    optimistic = BooleanProperty(False)

    __events__ = ("on_success", "on_failed", "on_done")

    def __init__(self, kind: StateOperationKind, *, optimistic: bool) -> None:
        super().__init__()

        self.id = uuid4().hex
        self.kind = kind
        self.optimistic = optimistic

        self._loop = asyncio.get_running_loop()
        self._completion = self._loop.create_future()

    def __await__(self):
        return self.wait().__await__()

    async def wait(self) -> StateOperation:
        await asyncio.shield(self._completion)
        return self

    def succeed(self) -> None:
        self._finish(None)

    def fail(self, failure: StateFailure) -> None:
        if not isinstance(failure, StateFailure):
            raise TypeError("failure must be a StateFailure")

        self._finish(failure)

    def _finish(self, failure: StateFailure | None) -> None:
        self._require_loop()

        if self.done:
            return

        self.failure = failure
        self.done = True
        self.success = failure is None
        self.failed = failure is not None

        if failure is None:
            self.dispatch("on_success")
        else:
            self.dispatch("on_failed", failure)

        self.dispatch("on_done", failure)

        if not self._completion.done():
            self._completion.set_result(None)

    def _require_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise RuntimeError(
                "StateOperation must be completed from its asyncio loop"
            ) from error

        if loop is not self._loop:
            raise RuntimeError(
                "StateOperation was completed from a different asyncio loop"
            )

    def on_success(self) -> None:
        pass

    def on_failed(self, failure: StateFailure) -> None:
        pass

    def on_done(self, failure: StateFailure | None) -> None:
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
