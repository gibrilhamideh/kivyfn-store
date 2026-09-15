"""Deterministic State boundary harness; transport integration is separate."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from kivyfn.store.client.client import StoreClient
from kivyfn.store.protocol.message.event import EventMessage, SubscriptionEvent
from kivyfn.store.protocol.message.requests import (
    RequestAction,
    SetRowRequestAction,
)
from kivyfn.store.protocol.message.responses import TransactionResponse
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.fields import DataField, NumericField, StringField
from kivyfn.store.schema.signal import Signal
from kivyfn.store.state.manager import StateManager
from kivyfn.store.state.transaction.transaction import StateTransaction


class StateUnitRows(ManySchema):
    label = StringField(default="")
    value = NumericField(default=0)
    payload = DataField(default={})
    ping = Signal()


class StateUnitSingle(SingleSchema):
    value = NumericField(default=0)
    other = NumericField(default=0)
    ping = Signal()


class ControlledClient:
    def __init__(self) -> None:
        self.requests: dict[str, tuple[RequestAction, ...]] = {}
        self.futures: dict[str, asyncio.Future[Any]] = {}
        self.sources: set[Any] = set()
        self.metadata: dict[str, tuple[str | None, str | None]] = {}

    def subscribe(self, source: Any, manager: Any) -> None:
        self.sources.add(source)

    def unsubscribe(self, source: Any, manager: Any) -> None:
        self.sources.discard(source)

    def transaction(
        self,
        *,
        transaction_id: str,
        actions: tuple[RequestAction, ...],
        history_group: str | None = None,
        history_label: str | None = None,
    ) -> asyncio.Future[Any]:
        self.requests[transaction_id] = actions
        self.metadata[transaction_id] = (history_group, history_label)
        future = asyncio.get_running_loop().create_future()
        self.futures[transaction_id] = future
        return future

    def row_writes(
        self, tx: StateTransaction
    ) -> tuple[SetRowRequestAction, ...]:
        actions = self.requests[tx.id]
        assert all(
            isinstance(action, SetRowRequestAction) for action in actions
        )
        return cast(tuple[SetRowRequestAction, ...], actions)

    async def fail(self, tx: StateTransaction) -> None:
        self.futures[tx.id].set_exception(
            ConnectionError("controlled failure")
        )
        await asyncio.wait_for(tx.wait(), 1)

    async def succeed(
        self,
        manager: StateManager,
        tx: StateTransaction,
        *events: SubscriptionEvent,
    ) -> None:
        manager.on_subscription_message(EventMessage("test", events, tx.id))
        self.futures[tx.id].set_result(TransactionResponse(tx.id))
        await asyncio.wait_for(tx.wait(), 1)


def manager_pair() -> tuple[StateManager, ControlledClient]:
    client = ControlledClient()
    return StateManager(cast(StoreClient, client)), client
