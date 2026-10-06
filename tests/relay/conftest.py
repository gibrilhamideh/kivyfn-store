from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from tests.relay.helpers import Gateway, Stack, running_stack


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Gateway]:
    gateway = Gateway()
    await gateway.start()
    try:
        yield gateway
    finally:
        await gateway.stop()


@pytest_asyncio.fixture
async def relay_stack(
    tmp_path: Path, gateway: Gateway
) -> AsyncIterator[Stack]:
    async with running_stack(tmp_path, gateway) as stack:
        yield stack
