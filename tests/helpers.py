# tests/helpers.py
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from kivyfn.store import StoreClient
from kivyfn.store.protocol.message.requests import RequestBatch
from kivyfn.store.protocol.message.responses import ResponseBatch
from kivyfn.store.transport.client import ClientTransport
from kivyfn.store.transport.endpoint import create_client_transport

RequestSender = Callable[[RequestBatch], Awaitable[ResponseBatch]]


class InterceptedRequests:
    """Install request gates when the client's real transport is created."""

    def __init__(self) -> None:
        self.request: RequestSender = self.send
        self._send: RequestSender | None = None

    async def send(self, request: RequestBatch) -> ResponseBatch:
        assert self._send is not None
        return await self._send(request)

    async def _dispatch(self, request: RequestBatch) -> ResponseBatch:
        return await self.request(request)

    def attach(self, transport: ClientTransport) -> None:
        self._send = transport.request
        transport.request = self._dispatch


def intercept_client_requests(
    monkeypatch: pytest.MonkeyPatch, client: StoreClient
) -> InterceptedRequests:
    requests = InterceptedRequests()

    def create(endpoint: str, owner: StoreClient) -> ClientTransport:
        transport = create_client_transport(endpoint, owner)
        if owner is client:
            requests.attach(transport)
        return transport

    monkeypatch.setattr(
        "kivyfn.store.client.client.create_client_transport", create
    )
    return requests


def state_value(state: object, name: str) -> Any:
    return getattr(state, name)


def set_state_value(state: object, name: str, value: Any) -> None:
    setattr(state, name, value)


async def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 1.0
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)
