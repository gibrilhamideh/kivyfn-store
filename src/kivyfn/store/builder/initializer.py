from __future__ import annotations

from typing import Any


class Initializer:
    """Prepare initial Store data after migration and before validation.

    ``run()`` receives a detached dictionary keyed by schema name. Values
    are raw data: schema defaults and relationship checks have not run.
    Return the resulting dictionary, including any nonpersistent schemas
    to seed. Subclasses can organize preparation with ordinary helpers.

    Initialization establishes the startup baseline without transactions,
    triggers, signals or history entries. It runs for each new Store and
    again on empty data if ERASE needs its one fresh-default fallback.
    Exceptions and non-dictionary results fail startup without erasure.
    Configure a module-level subclass with serializable instance data when
    passing the Store configuration to a child process.
    """

    def run(self, data: dict[str, Any]) -> dict[str, Any]:
        """Prepare raw startup data; the default leaves it unchanged."""
        return data
