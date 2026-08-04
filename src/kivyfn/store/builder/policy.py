from enum import Enum


class BootPolicy(Enum):
    """
    Defines how persisted store data is handled during boot.

    -------------------------------------------------------------------------
    Problem                         RAISE          RECOVER        ERASE
    -------------------------------------------------------------------------
    Missing field                   Use default    Use default    Use default
    Extra field                     Remove         Remove         Remove
    Invalid field value             Raise          Use default    Use default
    Invalid row shape               Raise          Remove row     Remove row
    Index conflict                  Raise          Remove row     Remove row
    Missing foreign key             Raise          Remove/reset   Remove/reset
    Malformed schema payload        Raise          Reset node     Reset node
    Migration failure               Raise          Raise          Erase store
    Store version newer than app    Raise          Raise          Erase store
    Unexpected boot failure         Raise          Raise          Erase store
    -------------------------------------------------------------------------

    RAISE
        Strict boot mode.

        Missing fields are filled with their schema defaults and unknown
        fields are discarded while rebuilding the stored data.

        Invalid field values, malformed rows, index conflicts, missing
        foreign keys, malformed schema payloads, migration failures, newer
        persisted store versions, and unexpected boot failures are raised.

        Stored data is never selectively recovered or erased.

    RECOVER
        Selective recovery mode.

        Missing fields are filled with their schema defaults and unknown
        fields are discarded while rebuilding the stored data.

        Invalid field values are replaced with their schema defaults.
        Malformed rows and rows with index conflicts are removed.
        Missing foreign keys cause the affected row to be removed or the
        affected singleton node to be reset.
        Malformed schema payloads cause only the affected node to be reset.

        Migration failures, newer persisted store versions, and unexpected
        boot failures are raised.

        The entire store is never erased.

    ERASE
        Selective recovery with full-store erasure as the final fallback.

        It performs the same selective recovery as RECOVER for missing
        fields, unknown fields, invalid field values, malformed rows, index
        conflicts, missing foreign keys, and malformed schema payloads.

        If boot still fails because of a migration failure, a newer persisted
        store version, or another unexpected boot failure, the entire
        persistent store is erased and rebuilt using the current schema
        defaults and current store version.
    """

    RAISE = "raise"
    RECOVER = "recover"
    ERASE = "erase"
