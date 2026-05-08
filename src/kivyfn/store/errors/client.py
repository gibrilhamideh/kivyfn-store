class StoreClientError(Exception):
    pass


class StoreStateError(StoreClientError):
    pass


class StoreConnectionError(StoreClientError):
    pass
