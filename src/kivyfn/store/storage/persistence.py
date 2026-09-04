from __future__ import annotations

import asyncio
import copy
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from kivyfn.logging import get_logger


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


logger = get_logger(__name__)

_INTERVAL_PATTERN = re.compile(
    r"^(?P<hours>\d{2,}):(?P<minutes>[0-5]\d):"
    r"(?P<seconds>[0-5]\d(?:\.\d+)?)$"
)


class PersistenceFlushError(RuntimeError):
    """One or more dirty schemas could not be flushed."""


@dataclass(frozen=True, slots=True)
class PersistenceSnapshot:
    node: Node
    generation: int
    data: dict


@dataclass(slots=True)
class _PendingPersistence:
    node: Node
    interval: float
    generation: int
    deadline: float | None
    in_flight: bool = False


def parse_persist_interval(value: str | None) -> float | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise TypeError(
            "persist_interval must be an HH:MM:SS string or None"
        )

    match = _INTERVAL_PATTERN.fullmatch(value)

    if match is None:
        raise ValueError(
            "persist_interval must use HH:MM:SS format"
        )

    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = float(match.group("seconds"))
    total = hours * 3600.0 + minutes * 60.0 + seconds

    if total <= 0.0:
        raise ValueError("persist_interval must be greater than zero")

    return total


class PersistenceTracker:
    """Thread-safe dirty-state and deadline tracker."""

    def __init__(
        self,
        *,
        retry_delay: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if retry_delay <= 0.0:
            raise ValueError("retry_delay must be greater than zero")

        self._retry_delay = retry_delay
        self._clock = clock
        self._pending: dict[Node, _PendingPersistence] = {}
        self._wake: Callable[[], None] | None = None
        self._lock = threading.RLock()

    def set_waker(
        self,
        callback: Callable[[], None] | None,
    ) -> None:
        with self._lock:
            self._wake = callback

    def mark_dirty(
        self,
        node: Node,
        interval: float,
    ) -> None:
        if interval <= 0.0:
            raise ValueError("Persistence interval must be positive")

        callback: Callable[[], None] | None

        with self._lock:
            pending = self._pending.get(node)

            if pending is None:
                pending = _PendingPersistence(
                    node=node,
                    interval=interval,
                    generation=1,
                    deadline=self._clock() + interval,
                )
                self._pending[node] = pending
            else:
                pending.generation += 1

                if pending.deadline is None:
                    pending.deadline = self._clock() + interval

            callback = self._wake

        if callback is not None:
            callback()

    def capture_due(
        self,
        *,
        force: bool = False,
    ) -> tuple[PersistenceSnapshot, ...]:
        now = self._clock()
        snapshots: list[PersistenceSnapshot] = []

        with self._lock:
            pending_items = sorted(
                self._pending.values(),
                key=lambda item: (
                    float("inf")
                    if item.deadline is None
                    else item.deadline,
                    item.node.schema.name,
                ),
            )

            for pending in pending_items:
                if pending.in_flight:
                    continue

                if not force:
                    deadline = pending.deadline

                    if deadline is None or deadline > now:
                        continue

                pending.in_flight = True
                pending.deadline = None
                snapshots.append(
                    PersistenceSnapshot(
                        node=pending.node,
                        generation=pending.generation,
                        data=copy.deepcopy(pending.node.value),
                    )
                )

        return tuple(snapshots)

    def complete(
        self,
        snapshot: PersistenceSnapshot,
        *,
        success: bool,
    ) -> None:
        callback: Callable[[], None] | None = None

        with self._lock:
            pending = self._pending.get(snapshot.node)

            if pending is None:
                return

            pending.in_flight = False

            if success and pending.generation == snapshot.generation:
                self._pending.pop(snapshot.node, None)
                return

            if success:
                if pending.deadline is None:
                    pending.deadline = self._clock() + pending.interval
            else:
                pending.deadline = self._clock() + self._retry_delay

            callback = self._wake

        if callback is not None:
            callback()

    def next_deadline(self) -> float | None:
        with self._lock:
            deadlines = [
                pending.deadline
                for pending in self._pending.values()
                if not pending.in_flight
                and pending.deadline is not None
            ]

        if not deadlines:
            return None

        return min(deadlines)

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


class ThreadPersistenceCoordinator:
    """One persistence scheduler for a synchronous LocalStore."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._failure: BaseException | None = None
        self._flush_on_stop = True
        self._lock = threading.RLock()

    def start(self) -> None:
        if not self._engine.has_delayed_persistence:
            return

        with self._lock:
            if self._thread is not None:
                return

            self._stopping = False
            self._failure = None
            self._flush_on_stop = True
            self._wake.clear()
            self._engine.set_persistence_waker(self._wake.set)
            self._thread = threading.Thread(
                target=self._run,
                name="kivyfn-store-persistence",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, flush: bool = True) -> None:
        with self._lock:
            thread = self._thread

            if thread is None:
                if flush:
                    self._flush_all()
                return

            self._stopping = True
            self._flush_on_stop = flush
            self._wake.set()

        thread.join()

        with self._lock:
            self._thread = None
            self._engine.set_persistence_waker(None)
            failure = self._failure
            self._failure = None

        if failure is not None:
            raise PersistenceFlushError(
                "Could not flush all delayed schemas"
            ) from failure

    def _run(self) -> None:
        try:
            while not self._stopping:
                self._flush_due()
                timeout = self._wait_timeout()
                self._wake.wait(timeout)
                self._wake.clear()

            if self._flush_on_stop:
                self._flush_all()
        except BaseException as exc:
            logger.exception("Persistence coordinator stopped unexpectedly")
            self._failure = exc

    def _wait_timeout(self) -> float | None:
        deadline = self._engine.next_persistence_deadline()

        if deadline is None:
            return None

        return max(0.0, deadline - time.monotonic())

    def _flush_due(self) -> None:
        for snapshot in self._engine.capture_due_persistence():
            self._write_snapshot(snapshot, raise_errors=False)

    def _flush_all(self) -> None:
        while self._engine.has_pending_persistence:
            snapshots = self._engine.capture_due_persistence(
                force=True,
            )

            if not snapshots:
                raise PersistenceFlushError(
                    "Dirty schemas could not be captured for shutdown"
                )

            for snapshot in snapshots:
                self._write_snapshot(snapshot, raise_errors=True)

    def _write_snapshot(
        self,
        snapshot: PersistenceSnapshot,
        *,
        raise_errors: bool,
    ) -> None:
        try:
            self._engine.write_persistence_snapshot(snapshot)
        except BaseException:
            self._engine.complete_persistence_snapshot(
                snapshot,
                success=False,
            )

            if raise_errors:
                raise

            logger.exception(
                "Delayed persistence failed for schema %s; retrying.",
                snapshot.node.schema.name,
            )
            return

        self._engine.complete_persistence_snapshot(
            snapshot,
            success=True,
        )


class AsyncPersistenceCoordinator:
    """One persistence scheduler for an asynchronous StoreServer."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._flush_on_stop = True

    def start(self) -> None:
        if not self._engine.has_delayed_persistence:
            return

        if self._task is not None:
            return

        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stopping = False
        self._flush_on_stop = True
        self._engine.set_persistence_waker(self._wake_threadsafe)
        self._task = asyncio.create_task(
            self._run(),
            name="kivyfn-store-persistence",
        )

    async def stop(self, *, flush: bool = True) -> None:
        task = self._task

        if task is None:
            if flush:
                await self._flush_all()
            return

        self._stopping = True
        self._flush_on_stop = flush
        self._wake_threadsafe()

        try:
            await task
        finally:
            self._task = None
            self._wake = None
            self._loop = None
            self._engine.set_persistence_waker(None)

    def _wake_threadsafe(self) -> None:
        loop = self._loop
        wake = self._wake

        if loop is None or wake is None or loop.is_closed():
            return

        loop.call_soon_threadsafe(wake.set)

    async def _run(self) -> None:
        while not self._stopping:
            await self._flush_due()
            await self._wait()

        if self._flush_on_stop:
            await self._flush_all()

    async def _wait(self) -> None:
        wake = self._wake

        if wake is None:
            return

        wake.clear()
        deadline = self._engine.next_persistence_deadline()

        if deadline is None:
            await wake.wait()
            return

        timeout = max(0.0, deadline - time.monotonic())

        try:
            await asyncio.wait_for(wake.wait(), timeout=timeout)
        except TimeoutError:
            pass

    async def _flush_due(self) -> None:
        snapshots = self._engine.capture_due_persistence()

        for snapshot in snapshots:
            await self._write_snapshot(snapshot, raise_errors=False)

    async def _flush_all(self) -> None:
        while self._engine.has_pending_persistence:
            snapshots = self._engine.capture_due_persistence(
                force=True,
            )

            if not snapshots:
                raise PersistenceFlushError(
                    "Dirty schemas could not be captured for shutdown"
                )

            for snapshot in snapshots:
                await self._write_snapshot(
                    snapshot,
                    raise_errors=True,
                )

    async def _write_snapshot(
        self,
        snapshot: PersistenceSnapshot,
        *,
        raise_errors: bool,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._engine.write_persistence_snapshot,
                snapshot,
            )
        except BaseException:
            self._engine.complete_persistence_snapshot(
                snapshot,
                success=False,
            )

            if raise_errors:
                raise

            logger.exception(
                "Delayed persistence failed for schema %s; retrying.",
                snapshot.node.schema.name,
            )
            return

        self._engine.complete_persistence_snapshot(
            snapshot,
            success=True,
        )
