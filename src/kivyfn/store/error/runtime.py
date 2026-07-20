# kivyfn/store/error/runtime.py
from __future__ import annotations

from kivyfn.store.error.base import StoreRuntimeError


class StoreStateError(StoreRuntimeError):
    """
    A store operation was requested while the store was in an invalid
    lifecycle state.

    Examples:

    - LocalStore has not been booted.
    - LocalStore has already been shut down.
    - RemoteStore is not connected.
    - A connection or boot operation is requested in an invalid state.
    """


class StoreUnavailableError(StoreRuntimeError):
    """
    The store cannot currently be reached or used.

    This is the broad base class for availability failures such as connection
    failures and request timeouts.
    """


class StoreConnectionError(StoreUnavailableError):
    """
    A remote store connection could not be established or was lost before an
    operation completed.
    """


class StoreTimeoutError(StoreUnavailableError):
    """
    A remote store operation did not complete within the configured timeout.
    """


class StoreProtocolError(StoreRuntimeError):
    """
    A remote peer or transport produced data that violates the store protocol.

    Examples:

    - malformed response payload;
    - unknown response version;
    - missing required response fields;
    - invalid serialized error data.
    """


class StoreInternalError(StoreRuntimeError):
    """
    A remote store reported an unexpected internal failure.

    The original server exception should remain private and be logged on the
    server. This exception may contain a safe diagnostic message or request
    identifier, but should not expose an internal traceback.
    """
