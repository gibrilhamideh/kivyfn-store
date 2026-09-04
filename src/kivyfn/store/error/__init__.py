# kivyfn/store/error/__init__.py
from kivyfn.store.error.base import (
    StoreException,
    StoreOperationError,
    StoreRuntimeError,
)
from kivyfn.store.error.category import StoreErrorCategory
from kivyfn.store.error.code import StoreErrorCode
from kivyfn.store.error.codec import (
    decode_failure,
    encode_failure,
)
from kivyfn.store.error.context import StoreErrorContext
from kivyfn.store.error.detail import (
    AddressErrorDetail,
    ConstraintErrorDetail,
    FieldErrorDetail,
    IndexErrorDetail,
    InstructionErrorDetail,
    NodeErrorDetail,
    ProtocolValue,
    RowErrorDetail,
    RuntimeErrorDetail,
    StoreErrorDetail,
)
from kivyfn.store.error.failure import StoreFailure
from kivyfn.store.error.operation import (
    AddressNotFoundError,
    ConstraintError,
    FieldError,
    FieldNotFoundError,
    FieldValidationError,
    ForeignKeyNotFoundError,
    ForeignKeyRestrictedError,
    IndexNotFoundError,
    InvalidInstructionError,
    InvalidOperationError,
    NodeNotFoundError,
    RowAlreadyExistsError,
    RowError,
    RowNotFoundError,
    RowOperationConflictError,
    UniqueConstraintError,
)
from kivyfn.store.error.runtime import (
    StoreTransportError,
    StoreConnectionError,
    StoreInternalError,
    StorageCorruptionError,
    StoreProtocolError,
    StoreStateError,
    StoreTimeoutError,
    StoreUnavailableError,
)
from kivyfn.store.error.stage import StoreErrorStage
from kivyfn.store.error.status import StoreErrorStatus


__all__ = [
    "AddressErrorDetail",
    "AddressNotFoundError",
    "ConstraintError",
    "ConstraintErrorDetail",
    "FieldError",
    "FieldErrorDetail",
    "FieldNotFoundError",
    "FieldValidationError",
    "ForeignKeyNotFoundError",
    "ForeignKeyRestrictedError",
    "IndexErrorDetail",
    "IndexNotFoundError",
    "InstructionErrorDetail",
    "InvalidInstructionError",
    "InvalidOperationError",
    "NodeErrorDetail",
    "NodeNotFoundError",
    "ProtocolValue",
    "RowAlreadyExistsError",
    "RowError",
    "RowErrorDetail",
    "RowNotFoundError",
    "RowOperationConflictError",
    "RuntimeErrorDetail",
    "StoreConnectionError",
    "StoreErrorCategory",
    "StoreErrorCode",
    "StoreErrorContext",
    "StoreErrorDetail",
    "StoreErrorStage",
    "StoreErrorStatus",
    "StoreException",
    "StoreFailure",
    "StoreInternalError",
    "StoreOperationError",
    "StorageCorruptionError",
    "StoreProtocolError",
    "StoreRuntimeError",
    "StoreStateError",
    "StoreTransportError",
    "StoreTimeoutError",
    "StoreUnavailableError",
    "UniqueConstraintError",
    "decode_failure",
    "encode_failure",
]
