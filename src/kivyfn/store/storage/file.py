from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path


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

    def read(self, name: str) -> dict | None:
        path = self._file_path(name)

        if not path.exists():
            return None

        if path.stat().st_size == 0:
            return None

        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except JSONDecodeError:
            return None

    # -------------------------------------------------------
    # Write
    # -------------------------------------------------------

    def write(self, schema_name: str, data: dict, *, store_version: int) -> None:
        path = self._file_path(schema_name)

        payload = {
            "metadata": {
                "store_version": store_version,
            },
            "data": data,
        }

        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    # -------------------------------------------------------
    # Exists
    # -------------------------------------------------------
    def exists(self, schema_name: str) -> bool:
        return self._file_path(schema_name).exists()

    # -------------------------------------------------------
    # Load Store
    # -------------------------------------------------------
    def load_store(self, schema_names: list[str]) -> dict[str, dict | None]:
        store = {}

        for schema_name in schema_names:
            store[schema_name] = self.read(schema_name)

        return store
