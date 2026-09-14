# tests/helpers.py
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


def state_value(state: object, name: str) -> Any:
    return getattr(state, name)


def set_state_value(state: object, name: str, value: Any) -> None:
    setattr(state, name, value)


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)
