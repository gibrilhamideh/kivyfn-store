# kivyfn/store/error/code.py
from __future__ import annotations

from enum import StrEnum


class StoreErrorCode(StrEnum):
    """
    Stable machine-readable codes for store failures.

    Error codes are part of the public store protocol and must remain
    transport-independent. Application logic may use these values to handle
    failures consistently across LocalStore and RemoteStore.

    Human-readable error messages are diagnostic and may change. Error codes
    should remain stable once the framework is public.
    """

    # -------------------------------------------------
    # Instructions and operations
    # -------------------------------------------------

    INVALID_INSTRUCTION = "invalid_instruction"
    INVALID_OPERATION = "invalid_operation"

    # -------------------------------------------------
    # Address tree
    # -------------------------------------------------

    ADDRESS_NOT_FOUND = "address_not_found"
    NODE_NOT_FOUND = "node_not_found"

    # -------------------------------------------------
    # Fields
    # -------------------------------------------------

    FIELD_NOT_FOUND = "field_not_found"
    FIELD_VALIDATION_FAILED = "field_validation_failed"

    # -------------------------------------------------
    # Rows
    # -------------------------------------------------

    ROW_NOT_FOUND = "row_not_found"
    ROW_ALREADY_EXISTS = "row_already_exists"
    ROW_OPERATION_CONFLICT = "row_operation_conflict"

    # -------------------------------------------------
    # Indexes
    # -------------------------------------------------

    INDEX_NOT_FOUND = "index_not_found"

    # -------------------------------------------------
    # Constraints
    # -------------------------------------------------

    UNIQUE_CONSTRAINT_FAILED = "unique_constraint_failed"
    FOREIGN_KEY_NOT_FOUND = "foreign_key_not_found"
    FOREIGN_KEY_RESTRICTED = "foreign_key_restricted"

    # -------------------------------------------------
    # Store runtime
    # -------------------------------------------------

    STORE_STATE_ERROR = "store_state_error"
    STORE_UNAVAILABLE = "store_unavailable"
    STORE_TIMEOUT = "store_timeout"

    # -------------------------------------------------
    # Protocol
    # -------------------------------------------------

    PROTOCOL_ERROR = "protocol_error"

    # -------------------------------------------------
    # Internal failures
    # -------------------------------------------------

    INTERNAL_ERROR = "internal_error"
