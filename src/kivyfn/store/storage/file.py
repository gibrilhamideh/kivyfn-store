from __future__ import annotations

import json
import os
from json import JSONDecodeError
from pathlib import Path
from threading import RLock
from typing import Any

from kivyfn.store.error import StorageCorruptionError


class FileStorage:
    """Crash-safe JSON persistence with one file per schema."""

    __slots__ = ("_base_path", "_lock")

    def __init__(self, base_path: str | Path) -> None:
        self._base_path = Path(base_path)
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    # -------------------------------------------------------
    # Path resolution
    # -------------------------------------------------------

    def _file_path(self, schema_name: str) -> Path:
        return self._base_path / f"{schema_name}.json"

    def _temp_path(self, schema_name: str) -> Path:
        path = self._file_path(schema_name)
        return path.with_suffix(path.suffix + ".tmp")

    # -------------------------------------------------------
    # Read
    # -------------------------------------------------------

    def read(self, name: str) -> Any | None:
        with self._lock:
            path = self._file_path(name)
            temp_path = self._temp_path(name)

            live_exists, live_valid, live_data = self._read_candidate(path)
            temp_exists, temp_valid, temp_data = self._read_candidate(
                temp_path,
            )

            if live_valid:
                if temp_exists:
                    self._remove_temp(temp_path)

                return live_data

            if temp_valid:
                os.replace(temp_path, path)
                self._fsync_directory()
                return temp_data

            if not live_exists and not temp_exists:
                return None

            raise StorageCorruptionError(
                f"No valid storage snapshot exists for schema {name!r}"
            )

    def _read_candidate(
        self,
        path: Path,
    ) -> tuple[bool, bool, Any | None]:
        if not path.exists():
            return False, False, None

        try:
            with path.open("r", encoding="utf-8") as file:
                return True, True, json.load(file)
        except (JSONDecodeError, UnicodeError):
            return True, False, None

    # -------------------------------------------------------
    # Write
    # -------------------------------------------------------

    def write(
        self,
        schema_name: str,
        data: dict,
        *,
        store_version: int,
    ) -> None:
        payload = {
            "metadata": {
                "store_version": store_version,
            },
            "data": data,
        }

        with self._lock:
            path = self._file_path(schema_name)
            temp_path = self._temp_path(schema_name)

            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(payload, file, indent=2)
                file.flush()
                os.fsync(file.fileno())

            os.replace(temp_path, path)
            self._fsync_directory()

    def _remove_temp(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return

        self._fsync_directory()

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self._base_path, flags)

        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    # -------------------------------------------------------
    # Exists
    # -------------------------------------------------------

    def exists(self, schema_name: str) -> bool:
        with self._lock:
            return self._file_path(schema_name).exists()

    # -------------------------------------------------------
    # Load store
    # -------------------------------------------------------

    def load_store(
        self,
        schema_names: list[str],
    ) -> dict[str, Any | None]:
        return {
            schema_name: self.read(schema_name)
            for schema_name in schema_names
        }
