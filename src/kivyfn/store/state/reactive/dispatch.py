"""Stage observable updates before delivering application callbacks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from kivyfn.logging import get_logger

if TYPE_CHECKING:
    from kivyfn.store.state.reactive.state import State

logger = get_logger(__name__)


class NotificationBatch:
    """Collect status, field, and membership notifications in delivery order.

    Values change immediately. Only callbacks wait. The client may prepare
    several managers' batches before publishing any of them; signals follow
    all retained notifications through SubscriptionPublication.
    """

    def __init__(self) -> None:
        self.properties: dict[tuple[Any, str], Any] = {}
        self.fields: dict[State[Any], set[str]] = {}
        self.callbacks: list[Callable[[], None]] = []

    def publish(self) -> None:
        for (target, name), value in self.properties.items():
            set_property(target, name, value)
        for state, fields in self.fields.items():
            state.publish(fields)
        for callback in self.callbacks:
            callback()


_batch: ContextVar[NotificationBatch | None] = ContextVar(
    "state_notifications", default=None
)


@contextmanager
def collect_notifications() -> Iterator[NotificationBatch]:
    """Capture one delivery without publishing it on exit.

    Used by the manager while the client stages a complete message across
    subscriptions. The caller publishes the returned batch after staging.
    """
    batch = NotificationBatch()
    token = _batch.set(batch)
    try:
        yield batch
    finally:
        _batch.reset(token)


@contextmanager
def notification_batch() -> Iterator[None]:
    """Publish a local update on successful exit, or join the current batch.

    This batches notifications, not storage rollback. Transaction failure
    handling is responsible for removing optimistic changes after an error.
    """
    if _batch.get() is not None:
        yield
        return
    with collect_notifications() as batch:
        yield
    batch.publish()


def notify_fields(state: State[Any], fields: frozenset[str]) -> None:
    if not fields:
        return
    batch = _batch.get()
    if batch is None:
        state.publish(fields)
    else:
        batch.fields.setdefault(state, set()).update(fields)


def notify(callback: Callable[[], None]) -> None:
    batch = _batch.get()
    if batch is None:
        callback()
    else:
        batch.callbacks.append(callback)


def dispatch(target: Any, event: str, *args: Any) -> None:
    try:
        target.dispatch(event, *args)
    except Exception:
        logger.exception("State callback failed: event=%s", event)


def set_property(target: Any, name: str, value: Any) -> None:
    batch = _batch.get()
    if batch is not None:
        batch.properties[(target, name)] = value
        return
    try:
        setattr(target, name, value)
    except Exception:
        logger.exception("State property callback failed: property=%s", name)
