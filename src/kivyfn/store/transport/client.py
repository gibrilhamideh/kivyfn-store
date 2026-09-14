# src/kivyfn/store/transport/client.py
from __future__ import annotations

from typing import Protocol

from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch


class ClientTransport(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def request(self, request: RequestBatch) -> ResponseBatch: ...
