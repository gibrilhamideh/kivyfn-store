# src/kivyfn/store/builder/policy.py
from enum import Enum


class BootPolicy(Enum):
    """Control validation and recovery of Store boot data.

    Boot loads the complete generation named by the Store manifest. Legacy
    per-schema JSON snapshots are imported at their highest valid saved Store
    version.
    Lower-version snapshots are excluded. Missing or excluded schemas use
    their current defaults. Nonpersistent nodes also start at defaults unless
    the configured initializer supplies their initial data.

    Migration classes declare their target version. Boot checks the complete
    forward chain before running any step, then migrates a working copy.
    Missing, invalid, or duplicate migration declarations, an unsupported
    downgrade, or a failed migration are logged. Boot then applies this
    policy to the original selected data. A successful migration passes its
    result to the same initialization, validation and recovery process.

    Migration being skipped or failing does not itself make boot fail.

    An optional Initializer prepares a detached raw data dictionary after
    migration and before defaults, fields, indexes and relationships are
    checked. Its output may seed persistent and nonpersistent schemas. Its
    exceptions and non-dictionary results fail startup under every policy;
    they are application faults, not reasons to discard saved data. Invalid
    schema data in its result follows the policy below. Incremental recovery
    does not rerun the initializer or recalculate its derived values.

    -------------------------------------------------------------------------
    Problem                         RAISE          RECOVER        ERASE
    -------------------------------------------------------------------------
    Missing schema                  Use defaults   Use defaults   Use defaults
    Missing field                   Use default    Use default    Use default
    Extra field                     Remove         Remove         Remove
    Invalid field value             Raise          Use default    Use default
    Invalid row shape               Raise          Remove row     Remove row
    Index conflict                  Raise          Remove row     Remove row
    Missing foreign key             Raise          Remove/reset   Remove/reset
    Malformed schema payload        Raise          Reset node     Reset node
    Invalid saved version metadata  Raise          Reset node     Reset node
    Unreadable schema JSON          Raise          Reset node     Reset node
    Unrecoverable build/validation   Raise          Raise          Reset store
    Initializer callback failure    Raise          Raise          Raise
    Filesystem I/O failure          Raise          Raise          Raise
    -------------------------------------------------------------------------

    RAISE
        Strict boot mode.

        Missing fields are filled with their schema defaults and unknown
        fields are discarded while rebuilding the stored data.

        Invalid values, malformed payloads or version metadata, unreadable
        schema JSON, index conflicts, and missing foreign keys cause boot
        to fail. Field and row recovery and a full Store reset are disabled.

    RECOVER
        Selective recovery mode.

        Preserve valid values and replace invalid fields with valid defaults.
        Remove malformed rows, rows whose fields cannot be recovered, and
        rows with index conflicts. Reset affected nodes for malformed
        payloads, invalid version metadata, or unreadable schema JSON.

        Missing foreign keys remove affected collection rows or reset the
        affected singleton. References are checked again after recovery.
        If recovery cannot produce a valid Store, boot fails without a
        full Store reset.

    ERASE
        Selective recovery with a full Store reset as the final fallback.

        First perform the same recovery as RECOVER. If building or validating
        the Store still fails, prepare a fresh candidate by running the
        initializer on empty data, then build every node with defaults for
        missing fields. This one fallback must pass strict field validation
        and existing foreign-key recovery before it can be saved. Invalid
        defaults or fallback initializer failures still cause boot to fail.

    After successful validation, save all persistent schemas in one complete
    generation and atomically switch the manifest at the target Store version.
    Remove imported and obsolete legacy snapshots only after durable success,
    preserving unrelated files. Retain the current and previous generations.

    Corrupt legacy schemas can be recovered independently. An unreadable
    shared manifest or generation resets the Store under RECOVER/ERASE;
    RAISE preserves the malformed files and fails. Uncommitted generations
    and manifest temporary files are ignored.

    Filesystem failures before the manifest switch propagate under every
    policy without ERASE recovery or cleanup. A directory fsync failure after
    the switch is logged as uncertain power-loss durability: the new complete
    generation is already committed and remains active. Both generations and
    imported legacy files are retained. Post-commit cleanup failures are also
    logged and cannot turn a successful commit into a failed transaction.
    """

    RAISE = "raise"
    RECOVER = "recover"
    ERASE = "erase"
