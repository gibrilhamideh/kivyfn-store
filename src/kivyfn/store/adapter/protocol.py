from __future__ import annotations

from typing import Any, Protocol

from kivyfn.store.address import Address


class SynchronizingAdapter(Protocol):
    """Adapter operations required by RemoteStoreClient recovery."""

    @property
    def address(self) -> Address[Any]:
        ...

    @property
    def loaded(self) -> bool:
        ...

    @property
    def synchronized(self) -> bool:
        ...

    @property
    def server_instance_id(self) -> str | None:
        ...

    @property
    def revision(self) -> int | None:
        ...

    def mark_sync_lost(
        self,
        reason: str,
    ) -> None:
        ...

    async def resync(
        self,
        reason: str = "manual",
    ) -> None:
        ...
