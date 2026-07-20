from __future__ import annotations
from typing import TypeAlias, TypeVar

T = TypeVar("T")

from kivyfn.store.local.subscription import LocalSubscription
from kivyfn.store.remote.subscription import RemoteSubscription
from kivyfn.store.transaction import TransactionResult
from kivyfn.store.response import Response

Subscription = LocalSubscription | RemoteSubscription

SubscriptionResponse: TypeAlias = Response[Subscription]
TransactionResponse: TypeAlias = Response[TransactionResult]
UnsubscriptionResponse: TypeAlias = Response[bool]
SnapshotResponse: TypeAlias = Response[T]
