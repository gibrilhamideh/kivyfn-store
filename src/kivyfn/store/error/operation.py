# kivyfn/store/error/operation.py
from __future__ import annotations

from kivyfn.store.error.base import StoreOperationError
from kivyfn.store.error.category import StoreErrorCategory
from kivyfn.store.error.code import StoreErrorCode
from kivyfn.store.error.status import StoreErrorStatus


# -------------------------------------------------
# Instructions and operations
# -------------------------------------------------


class InvalidInstructionError(StoreOperationError):
    """
    An instruction is malformed or missing required information.
    """

    code = StoreErrorCode.INVALID_INSTRUCTION
    category = StoreErrorCategory.INSTRUCTION
    status = StoreErrorStatus.INVALID


class InvalidOperationError(StoreOperationError):
    """
    An operation is not valid for the addressed schema or current state.
    """

    code = StoreErrorCode.INVALID_OPERATION
    category = StoreErrorCategory.INSTRUCTION
    status = StoreErrorStatus.INVALID


# -------------------------------------------------
# Address tree
# -------------------------------------------------


class AddressNotFoundError(StoreOperationError):
    """
    The requested address cannot be resolved in the store tree.
    """

    code = StoreErrorCode.ADDRESS_NOT_FOUND
    category = StoreErrorCategory.ADDRESS
    status = StoreErrorStatus.NOT_FOUND


class NodeNotFoundError(StoreOperationError):
    """
    No store node exists for the requested address or schema.
    """

    code = StoreErrorCode.NODE_NOT_FOUND
    category = StoreErrorCategory.ADDRESS
    status = StoreErrorStatus.NOT_FOUND


# -------------------------------------------------
# Fields
# -------------------------------------------------


class FieldError(StoreOperationError):
    """
    Base exception for field-related operation failures.
    """


class FieldNotFoundError(FieldError):
    """
    The requested field does not exist in the schema.
    """

    code = StoreErrorCode.FIELD_NOT_FOUND
    category = StoreErrorCategory.FIELD
    status = StoreErrorStatus.NOT_FOUND


class FieldValidationError(FieldError):
    """
    A field value failed schema validation.
    """

    code = StoreErrorCode.FIELD_VALIDATION_FAILED
    category = StoreErrorCategory.FIELD
    status = StoreErrorStatus.INVALID


# -------------------------------------------------
# Rows
# -------------------------------------------------


class RowError(StoreOperationError):
    """
    Base exception for row-related operation failures.
    """


class RowNotFoundError(RowError):
    """
    The requested row does not exist.
    """

    code = StoreErrorCode.ROW_NOT_FOUND
    category = StoreErrorCategory.ROW
    status = StoreErrorStatus.NOT_FOUND


class RowAlreadyExistsError(RowError):
    """
    A row already exists with the requested identifier.
    """

    code = StoreErrorCode.ROW_ALREADY_EXISTS
    category = StoreErrorCategory.ROW
    status = StoreErrorStatus.CONFLICT


class RowOperationConflictError(RowError):
    """
    Multiple operations produce an invalid or contradictory row state.
    """

    code = StoreErrorCode.ROW_OPERATION_CONFLICT
    category = StoreErrorCategory.ROW
    status = StoreErrorStatus.CONFLICT


# -------------------------------------------------
# Indexes
# -------------------------------------------------


class IndexNotFoundError(StoreOperationError):
    """
    No index exists for the requested field combination.
    """

    code = StoreErrorCode.INDEX_NOT_FOUND
    category = StoreErrorCategory.INDEX
    status = StoreErrorStatus.NOT_FOUND


# -------------------------------------------------
# Constraints
# -------------------------------------------------


class ConstraintError(StoreOperationError):
    """
    Base exception for relational constraint failures.
    """


class UniqueConstraintError(ConstraintError):
    """
    A value conflicts with an existing unique value.
    """

    code = StoreErrorCode.UNIQUE_CONSTRAINT_FAILED
    category = StoreErrorCategory.CONSTRAINT
    status = StoreErrorStatus.CONFLICT


class ForeignKeyNotFoundError(ConstraintError):
    """
    A foreign-key value references a row that does not exist.
    """

    code = StoreErrorCode.FOREIGN_KEY_NOT_FOUND
    category = StoreErrorCategory.CONSTRAINT
    status = StoreErrorStatus.CONFLICT


class ForeignKeyRestrictedError(ConstraintError):
    """
    An operation is blocked because another row still references its target.
    """

    code = StoreErrorCode.FOREIGN_KEY_RESTRICTED
    category = StoreErrorCategory.CONSTRAINT
    status = StoreErrorStatus.CONFLICT
