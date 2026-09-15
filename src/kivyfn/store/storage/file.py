from __future__ import annotations

import json
import os
from json import JSONDecodeError
from logging import getLogger as get_logger
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from kivyfn.store.error import StorageCorruptionError

logger = get_logger(__name__)


def _invalid_constant(value: str) -> None:
    raise StorageCorruptionError(f"Invalid JSON numeric constant: {value}")


class FileStorage:
    """Persist complete Store generations through one atomic manifest.

    Each generation contains every persistent schema. Only ``current.json``
    makes a generation committed; unfinished files are never promoted.
    Legacy per-schema snapshots remain readable until their first successful
    full commit. One Store owns a storage directory at a time.
    """

    __slots__ = ("_base_path", "_directory", "_lock")

    def __init__(self, base_path: str | Path) -> None:
        self._base_path = Path(base_path)
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._directory = self._base_path / ".kivyfn-store"
        self._lock = RLock()

    def _file_path(self, schema_name: str) -> Path:
        if not schema_name or Path(schema_name).name != schema_name:
            raise ValueError("Schema snapshot name must be a file name")
        return self._base_path / f"{schema_name}.json"

    def _temp_path(self, schema_name: str) -> Path:
        return self._file_path(schema_name).with_suffix(".json.tmp")

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file, parse_constant=_invalid_constant)
        except (JSONDecodeError, UnicodeError) as exc:
            raise StorageCorruptionError(
                f"Unreadable Store snapshot {path.name!r}"
            ) from exc

    @staticmethod
    def _validate_envelope(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise StorageCorruptionError("Store snapshot must be a dictionary")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise StorageCorruptionError("Store snapshot metadata is missing")
        version = metadata.get("store_version")
        if type(version) is not int or version < 1:
            raise StorageCorruptionError(f"Invalid store_version: {version!r}")
        if not isinstance(payload.get("data"), dict):
            raise StorageCorruptionError("Store snapshot data must be a map")

    def _read_legacy(self, name: str) -> Any | None:
        """Read a legacy snapshot, allowing recovery from its temp file."""
        invalid = False
        for path in (self._file_path(name), self._temp_path(name)):
            try:
                payload = self._read_json(path)
                self._validate_envelope(payload)
                return payload
            except FileNotFoundError:
                continue
            except StorageCorruptionError:
                invalid = True
        if invalid:
            raise StorageCorruptionError(
                f"No valid storage snapshot exists for schema {name!r}"
            )
        return None

    def _manifest_name(self) -> str | None:
        try:
            manifest = self._read_json(self._directory / "current.json")
        except FileNotFoundError:
            return None
        name = manifest.get("generation") if isinstance(manifest, dict) else ""
        if (
            not isinstance(name, str)
            or not name.startswith("generation-")
            or not name.endswith(".json")
            or Path(name).name != name
        ):
            raise StorageCorruptionError("Invalid Store generation manifest")
        return name

    def _load_generation(
        self, name: str, *, recover: bool = False
    ) -> dict[str, Any]:
        try:
            payload = self._read_json(self._directory / name)
        except FileNotFoundError as exc:
            raise StorageCorruptionError(
                f"Committed Store generation {name!r} is missing"
            ) from exc
        self._validate_envelope(payload)
        data = payload["data"]
        if not isinstance(data, dict):
            raise StorageCorruptionError("Store generation data must be a map")
        result = {}
        for schema, values in data.items():
            if not isinstance(values, dict):
                if not recover:
                    raise StorageCorruptionError(
                        f"Store snapshot data for {schema!r} must be a map"
                    )
                logger.error("Resetting malformed persisted schema %s", schema)
                continue
            result[schema] = {
                "metadata": payload["metadata"].copy(),
                "data": values,
            }
        return result

    def read(self, name: str) -> Any | None:
        with self._lock:
            generation = self._manifest_name()
            if generation is not None:
                return self._load_generation(generation).get(name)
            return self._read_legacy(name)

    def load_store(
        self, schema_names: list[str], *, recover: bool = False
    ) -> dict[str, Any]:
        """Load one committed generation or the legacy schema snapshots.

        Recovery may discard corrupt legacy schemas independently. Corruption
        of the shared generation/manifest cannot identify a safe committed
        subset and is reported to the boot policy as a Store-wide failure.
        """
        with self._lock:
            generation = self._manifest_name()
            if generation is not None:
                return self._load_generation(generation, recover=recover)
            names = set(schema_names)
            for path in self._base_path.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".json.tmp"):
                    name = path.name[:-9]
                elif path.name.endswith(".json"):
                    name = path.stem
                else:
                    continue
                try:
                    payload = self._read_json(path)
                except StorageCorruptionError:
                    continue
                if (
                    isinstance(payload, dict)
                    and "data" in payload
                    and isinstance(payload.get("metadata"), dict)
                    and "store_version" in payload["metadata"]
                ):
                    names.add(name)
            result: dict[str, Any] = {}
            for name in sorted(names):
                try:
                    payload = self._read_legacy(name)
                except StorageCorruptionError:
                    if not recover:
                        raise
                    logger.exception("Resetting unreadable schema %s", name)
                    continue
                if payload is not None:
                    result[name] = payload
            return result

    def write_store(
        self, data: dict[str, dict[str, Any]], *, store_version: int
    ) -> bool:
        """Commit all persistent schemas, or leave the previous commit active.

        Return whether the commit was durably synced.

        The manifest replacement is the commit point. A directory fsync error
        after replacement is logged: the transaction is already visible and
        must not be rolled back in memory. Its power-loss durability is then
        uncertain; both complete generations are retained.
        """
        payload = {
            "metadata": {"store_version": store_version},
            "data": {name: data[name] for name in sorted(data)},
        }
        self._validate_envelope(payload)
        encoded = json.dumps(payload, indent=2, allow_nan=False)
        with self._lock:
            try:
                previous = self._manifest_name()
            except StorageCorruptionError:
                # Boot recovery can replace a malformed manifest atomically.
                previous = None
            if not self._directory.exists():
                self._directory.mkdir()
            if previous is None:
                self._fsync_directory(self._base_path)
            name = f"generation-{uuid4().hex}.json"
            self._write_json(self._directory / name, encoded)
            self._fsync_directory(self._directory)
            temporary = self._directory / "current.json.tmp"
            self._write_json(temporary, json.dumps({"generation": name}))
            os.replace(temporary, self._directory / "current.json")
            try:
                self._fsync_directory(self._directory)
            except OSError:
                logger.exception(
                    "Store generation committed, but directory fsync failed; "
                    "power-loss durability is uncertain"
                )
                return False
            self._cleanup_generations({name, previous})
            return True

    @staticmethod
    def _write_json(path: Path, text: str) -> None:
        with path.open("w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())

    def _cleanup_generations(self, keep: set[str | None]) -> None:
        try:
            for path in self._directory.glob("generation-*.json"):
                if path.name not in keep:
                    path.unlink(missing_ok=True)
        except OSError:
            # Housekeeping cannot turn an already committed write into failure.
            logger.exception("Unable to remove obsolete Store generations")

    def cleanup_legacy(self, schema_names: set[str]) -> None:
        """Remove imported snapshots after a full generation is durable."""
        with self._lock:
            if self._manifest_name() is None:
                return
            try:
                # Also closes any durability uncertainty from a prior boot.
                self._fsync_directory(self._directory)
                for name in schema_names:
                    try:
                        path = self._file_path(name)
                    except ValueError:
                        # Generation names are JSON keys, not file paths.
                        continue
                    path.unlink(missing_ok=True)
                    self._temp_path(name).unlink(missing_ok=True)
                self._fsync_directory(self._base_path)
            except OSError:
                logger.exception("Unable to remove imported Store snapshots")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def exists(self, schema_name: str) -> bool:
        return self.read(schema_name) is not None

    def write(
        self, schema_name: str, data: dict, *, store_version: int
    ) -> None:
        """Update one schema while retaining all others in one generation.

        Engine and boot use ``write_store`` for atomic multi-schema changes.
        """
        with self._lock:
            saved = self.load_store([schema_name])
            versions = {
                value["metadata"]["store_version"] for value in saved.values()
            }
            if versions - {store_version}:
                raise ValueError("Use write_store to change the Store version")
            values = {name: payload["data"] for name, payload in saved.items()}
            values[schema_name] = data
            self.write_store(values, store_version=store_version)
