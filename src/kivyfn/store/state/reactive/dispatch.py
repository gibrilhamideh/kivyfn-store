"""Boundaries between framework progress and application callbacks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from kivyfn.logging import get_logger

logger = get_logger(__name__)

PropertyUpdates = dict[tuple[Any, str], Any]
_deferred: ContextVar[PropertyUpdates | None] = ContextVar(
    "state_deferred_properties", default=None
)


@contextmanager
def defer_properties() -> Iterator[PropertyUpdates]:
    """Collect property notifications while canonical state is being staged."""
    updates: PropertyUpdates = {}
    token = _deferred.set(updates)
    try:
        yield updates
    finally:
        _deferred.reset(token)


def dispatch(target: Any, event: str, *args: Any) -> None:
    try:
        target.dispatch(event, *args)
    except Exception:
        logger.exception("State callback failed: event=%s", event)


def set_property(target: Any, name: str, value: Any) -> None:
    updates = _deferred.get()
    if updates is not None:
        updates[(target, name)] = value
        return
    try:
        setattr(target, name, value)
    except Exception:
        logger.exception("State property callback failed: property=%s", name)
