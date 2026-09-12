from __future__ import annotations

# src/kivyfn/store/builder/migration.py
from __future__ import annotations

from typing import Any, ClassVar


class Migration:
    """Upgrade Store data from the previous version to ``version``.

    Subclasses declare an integer target version of at least two. For
    example, ``version = 3`` identifies the transition from v2 to v3.
    Configure migration instances in any order; boot resolves them by
    their declared target versions.

    ``run()`` receives the selected Store data keyed by schema name, with
    every snapshot at the preceding version. It must return a dictionary
    in the target version's format. Boot then applies its validation and
    recovery policy to the completed migration result.
    """

    version: ClassVar[int]

    def run(self, data: dict[str, Any]) -> dict[str, Any]:
        """Transform one Store version; the default leaves data unchanged."""
        return data
