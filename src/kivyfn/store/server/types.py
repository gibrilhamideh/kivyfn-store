# kivyfn/store/server/types.py
from __future__ import annotations

from typing import Callable, TypeAlias

from kivyfn.store.server.transport import StoreServerTransport


ServerTransportFactory: TypeAlias = Callable[
    ...,
    StoreServerTransport,
]
