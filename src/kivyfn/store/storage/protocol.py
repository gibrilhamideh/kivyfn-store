from __future__ import annotations
from typing import Protocol, Optional


class StateStorage(Protocol):
    """
    Protocol that all storage backends must implement.
    """

    def read(self, schema_name: str) -> Optional[dict]: ...

    def write(self, schema_name: str, data: dict) -> None: ...

    def exists(self, schema_name: str) -> bool: ...
