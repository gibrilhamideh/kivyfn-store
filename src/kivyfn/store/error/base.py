# kivyfn/store/error/base.py
from __future__ import annotations

from kivyfn.store.error.category import StoreErrorCategory
from kivyfn.store.error.code import StoreErrorCode
from kivyfn.store.error.context import StoreErrorContext
from kivyfn.store.error.detail import StoreErrorDetail
from kivyfn.store.error.failure import StoreFailure
from kivyfn.store.error.status import StoreErrorStatus

class StoreException(Exception):
    """
    Base exception for all intentional KivyFN Store exceptions.

    Expected operation failures and runtime failures inherit from separate
    subclasses so public store boundaries can handle them differently.
    """


class StoreOperationError(StoreException):
    """
    Expected store operation rejection.

    These exceptions are raised internally by the engine and converted into a
    StoreFailure inside a failed Response at the LocalStore or StoreServer
    boundary.
    """

    code: StoreErrorCode
    category: StoreErrorCategory
    status: StoreErrorStatus

    def __init__(
        self,
        message: str,
        *,
        detail: StoreErrorDetail | None = None,
        context: StoreErrorContext | None = None,
    ) -> None:
        if not isinstance(message, str) or not message:
            raise ValueError(
                "Store operation error message must be a non-empty string"
            )

        super().__init__(message)

        self.message = message
        self.detail = detail
        self.context = context

    def failure(self) -> StoreFailure:
        """
        Normalize this exception into the public transport-safe failure model.
        """
        return StoreFailure(
            code=self.code,
            category=self.category,
            status=self.status,
            message=self.message,
            detail=self.detail,
            context=self.context,
        )

    def with_context(
        self,
        context: StoreErrorContext,
    ) -> StoreOperationError:
        """
        Attach execution context while preserving the semantic error type.

        The existing exception is updated and returned so engine layers can add
        context without wrapping it in a broader exception.
        """
        self.context = context
        return self


class StoreRuntimeError(StoreException):
    """
    Failure that prevents a normal store Response from being produced.

    Examples include invalid lifecycle state, connection failure, request
    timeout, and malformed protocol data.
    """
