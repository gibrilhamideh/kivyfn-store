from kivyfn.store.host.query import HostQueries
from kivyfn.store.host.request import HostRequests
from kivyfn.store.host.runtime import HostRuntime
from kivyfn.store.host.subscription import HostSubscriptions
from kivyfn.store.protocol.message.requests import Request
from kivyfn.store.protocol.message.responses import Response
from kivyfn.store.store import Store
from kivyfn.store.transport.endpoint import create_host_transport


class StoreHost:
    """Compose the handlers that expose one Store through a transport."""

    def __init__(self, store: Store, endpoint: str) -> None:
        self.store = store
        self.endpoint = endpoint
        self.transport = create_host_transport(endpoint)
        self.queries = HostQueries(store)
        self.subscriptions = HostSubscriptions(store, self.queries)
        self.requests = HostRequests(store, self.subscriptions)
        self.runtime = HostRuntime(
            store, self.transport, self.requests, self.subscriptions
        )
        self.clients = self.runtime.clients

    @property
    def running(self) -> bool:
        return self.runtime.running

    async def start(self) -> None:
        await self.runtime.start(self)

    async def stop(self) -> None:
        await self.runtime.stop()

    async def connect(self, client_id: str) -> None:
        self.runtime.connect(client_id)

    async def request(self, client_id: str, request: Request) -> Response:
        return self.runtime.request(client_id, request)

    async def disconnect(self, client_id: str) -> None:
        await self.runtime.disconnect(client_id)
