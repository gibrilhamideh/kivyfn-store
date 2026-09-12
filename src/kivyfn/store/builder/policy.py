# src/kivyfn/store/builder/policy.py
from enum import Enum


class BootPolicy(Enum):
    """Control validation and recovery of Store boot data.

    Boot selects snapshots at the highest valid saved Store version.
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

    After successful validation, save new or changed persistent snapshots
    at the configured target version. Once all required saves succeed,
    delete obsolete Store snapshots while keeping unrelated files.

    Filesystem failures propagate under every policy. A saving failure
    stops boot without triggering ERASE recovery or obsolete-file cleanup.
    """

    RAISE = "raise"
    RECOVER = "recover"
    ERASE = "erase"
