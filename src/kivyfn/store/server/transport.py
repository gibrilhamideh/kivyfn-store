# kivyfn/store/server/transport.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol


if TYPE_CHECKING:
    from kivyfn.store.server.proxy import StoreServerProxy


class StoreServerTransport(Protocol):
    """
    Transport interface required by StoreServer.
    """

    async def start(
        self,
        proxy: StoreServerProxy,
    ) -> None:
        ...

    async def stop(self) -> None:
        ...

    @property
    def running(self) -> bool:
        ...

    @property
    def endpoint(self) -> dict[str, Any]:
        """
        Return the transport-specific connection information reported when a
        child server process becomes ready.
        """
        ...
