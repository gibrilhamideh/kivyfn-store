from __future__ import annotations

from typing import TypeAlias, TypeVar

from kivyfn.store.events.subscription import Subscription
from kivyfn.store.transaction import TransactionResult
from kivyfn.store.transport.response import Response


T = TypeVar("T")


TransactionResponse: TypeAlias = Response[TransactionResult]
SubscriptionResponse: TypeAlias = Response[Subscription]
UnsubscriptionResponse: TypeAlias = Response[bool]
SnapshotResponse: TypeAlias = Response[T]
