from __future__ import annotations
from enum import Enum, auto


class BootPolicyMode(Enum):
    """
    Defines how the engine should behave during boot
    when encountering invalid or inconsistent persisted data.
    """

    STRICT = auto()
    """
    Fail immediately on any validation error.
    No recovery. Raise FieldError / ConstraintError.
    """

    RECOVER_FIELDS = auto()
    """
    Attempt to normalize invalid field values
    (e.g. clamp min/max, apply defaults, drop unknown fields).
    """

    RECOVER_ROWS = auto()
    """
    For 'many' nodes, drop invalid rows entirely
    instead of failing boot.
    """

    BEST_EFFORT = auto()
    """
    Apply all recovery strategies possible:
    - Normalize fields
    - Drop invalid rows
    - Continue boot unless state is unrecoverable.
    """

