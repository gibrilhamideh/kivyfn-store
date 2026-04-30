# foundation/state/storage/file_store.py

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional


class FileStorage:
    """
    File-based persistence backend.
    Each schema is stored as one JSON file.
    """

    __slots__ = ("_base_path",)

    def __init__(self, base_path: str | Path):
        self._base_path = Path(base_path)
        self._base_path.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------
    # Path Resolution
    # -------------------------------------------------------

    def _file_path(self, schema_name: str) -> Path:
        return self._base_path / f"{schema_name}.json"

    # -------------------------------------------------------
    # Read
    # -------------------------------------------------------

    def read(self, schema_name: str) -> Optional[dict]:
        path = self._file_path(schema_name)

        if not path.exists():
            return None

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    # -------------------------------------------------------
    # Write
    # -------------------------------------------------------

    def write(self, schema_name: str, data: dict) -> None:
        path = self._file_path(schema_name)

        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # -------------------------------------------------------
    # Exists
    # -------------------------------------------------------

    def exists(self, schema_name: str) -> bool:
        return self._file_path(schema_name).exists()
