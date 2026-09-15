from __future__ import annotations

from kivyfn.store.error.base import StoreException
from kivyfn.store.error.failure import StoreFailure


class StoreSubscriptionError(StoreException):
    """The Host rejected a subscription on an otherwise usable connection."""

    def __init__(self, failure: StoreFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure
