# src/kivyfn/store/builder/policy.py
from enum import Enum


class BootPolicy(Enum):
    """Control validation and recovery of Store boot data.

    Boot loads the complete generation named by the Store manifest. Legacy
    per-schema JSON snapshots are imported at their highest valid saved Store
    version.
    Lower-version snapshots are excluded. Missing or excluded schemas use
    their current defaults, and nonpersistent nodes always start at defaults.

    Migration classes declare their target version. Boot checks the complete
    forward chain before running any step, then migrates a working copy.
    Missing, invalid, or duplicate migration declarations, an unsupported
    downgrade, or a failed migration are logged. Boot then applies this
    policy to the original selected data. A successful migration passes its
    result to the same validation and recovery process.

    Migration being skipped or failing does not itself make boot fail.

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
        the Store still fails, rebuild every node from its current defaults.
        The rebuilt Store must pass field and foreign-key validation before
        it can be saved. Invalid defaults still cause boot to fail.

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
