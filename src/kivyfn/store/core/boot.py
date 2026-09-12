# src/kivyfn/store/core/boot.py
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kivyfn.logging import get_logger
from kivyfn.store.builder.policy import BootPolicy
from kivyfn.store.error import (
    ConstraintErrorDetail,
    ForeignKeyNotFoundError,
    StorageCorruptionError,
)
from kivyfn.store.schema import ManySchema

if TYPE_CHECKING:
    from kivyfn.store.builder.migration import Migration
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


logger = get_logger(__name__)


class StoreBoot:
    """Migrate one saved Store version, then apply the boot policy.

    Only snapshots at the highest saved version enter migration. Missing or
    failed migrations leave that selected data available for boot recovery.
    Obsolete snapshots are removed after validation and successful saving.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._raw_store: dict[str, Any] = {}

    def boot(self) -> None:
        self._raw_store = self._load_store()

        try:
            data, current_version = self._select_data(self._raw_store)
            data = self._migrate(data, current_version)

            self._build_nodes(data)
            self._validate_foreign_keys()

        except Exception:
            if self.engine.config.boot_policy is not BootPolicy.ERASE:
                raise

            logger.exception(
                "Store boot could not be recovered; resetting Store defaults"
            )

            self._erase_and_boot_empty()

        # Disk failures must not trigger an ERASE fallback or cleanup.
        self._save_nodes()
        self._cleanup_store()

    # -------------------------------------------------
    # Policy
    # -------------------------------------------------

    @property
    def _recovering(self) -> bool:
        return self.engine.config.boot_policy in {
            BootPolicy.RECOVER,
            BootPolicy.ERASE,
        }

    # -------------------------------------------------
    # Load
    # -------------------------------------------------

    def _load_store(self) -> dict[str, Any]:
        schema_names = {
            node.schema.name for node in self.engine.metadata.persistent_nodes
        }
        candidates: set[str] = set()

        for path in Path(self.engine.config.storage_path).iterdir():
            if not path.is_file():
                continue

            if path.name.endswith(".json.tmp"):
                candidates.add(path.name[:-9])
            elif path.name.endswith(".json"):
                candidates.add(path.name[:-5])

        for schema_name in sorted(candidates - schema_names):
            if self._is_store_snapshot(schema_name):
                schema_names.add(schema_name)

        raw_store: dict[str, Any] = {}

        for schema_name in sorted(schema_names):
            try:
                raw_store[schema_name] = self.engine._storage.read(schema_name)
            except StorageCorruptionError:
                if not self._recovering:
                    raise

                logger.exception(
                    "Resetting unreadable persisted schema %s", schema_name
                )
                raw_store[schema_name] = None

        return raw_store

    def _is_store_snapshot(self, schema_name: str) -> bool:
        """Recognize retired schemas without claiming unrelated JSON files."""
        storage = self.engine._storage

        for path in (
            storage._file_path(schema_name),
            storage._temp_path(schema_name),
        ):
            _, valid, payload = storage._read_candidate(path)

            if not valid:
                continue

            if not isinstance(payload, dict) or "data" not in payload:
                return False

            metadata = payload.get("metadata")

            return isinstance(metadata, dict) and "store_version" in metadata

        return False

    def _select_data(
        self, raw_store: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        versions: dict[str, int] = {}

        for schema_name, payload in raw_store.items():
            if payload is None:
                continue

            try:
                versions[schema_name] = self._store_version(payload)
            except (TypeError, ValueError) as exc:
                if not self._recovering:
                    raise

                logger.error(
                    "Resetting persisted schema %s: %s", schema_name, exc
                )

        current_version = max(
            versions.values(), default=self.engine.config.store_version
        )
        data: dict[str, Any] = {}

        for schema_name, version in versions.items():
            if version != current_version:
                logger.warning(
                    "Ignoring persisted schema %s at v%s; selected Store v%s",
                    schema_name,
                    version,
                    current_version,
                )
                continue

            data[schema_name] = raw_store[schema_name].get("data")

        return data, current_version

    @staticmethod
    def _store_version(payload: Any) -> int:
        if not isinstance(payload, dict):
            raise TypeError("Persisted schema must contain a dictionary")

        metadata = payload.get("metadata", {})

        if not isinstance(metadata, dict):
            raise TypeError("Persisted schema metadata must be a dictionary")

        version = metadata.get("store_version", 1)

        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise ValueError(f"Invalid store_version: {version!r}")

        return version

    # -------------------------------------------------
    # Migration
    # -------------------------------------------------

    def _migrate(
        self, data: dict[str, Any], current_version: int
    ) -> dict[str, Any]:
        target_version = self.engine.config.store_version

        if current_version == target_version:
            return data

        if current_version > target_version:
            logger.error(
                "Migration skipped: no forward path from Store v%s to v%s; "
                "applying boot policy to the selected saved data",
                current_version,
                target_version,
            )
            return data

        migrations: dict[int, Migration] = {}

        for migration in self.engine.config.migrations:
            version = getattr(migration, "version", None)

            if (
                not isinstance(version, int)
                or isinstance(version, bool)
                or version < 2
            ):
                logger.error(
                    "Migration skipped: %s has invalid target version %r; "
                    "expected an integer of at least two; applying boot "
                    "policy to the selected saved data",
                    type(migration).__name__,
                    version,
                )
                return data

            error = None

            if version in migrations:
                error = f"duplicate migration classes for target v{version}"
            elif not callable(getattr(migration, "run", None)):
                error = f"migration target v{version} has no callable run()"

            if error is not None:
                logger.error(
                    "Migration skipped: %s; applying boot policy "
                    "to the selected saved data",
                    error,
                )
                return data

            migrations[version] = migration

        plan: list[tuple[int, Migration]] = []
        missing: list[str] = []

        for version in range(current_version, target_version):
            migration = migrations.get(version + 1)

            if migration is None:
                missing.append(f"v{version} -> v{version + 1}")
                continue

            plan.append((version, migration))

        if missing:
            logger.error(
                "Migration skipped: missing migration classes for %s; "
                "applying boot policy to the selected saved data",
                ", ".join(missing),
            )
            return data

        working_data = deepcopy(data)

        for version, migration in plan:
            try:
                working_data = migration.run(working_data)

                if not isinstance(working_data, dict):
                    raise TypeError("Migration.run() must return a dictionary")
            except Exception:
                logger.exception(
                    "Migration v%s -> v%s failed; applying boot policy "
                    "to the original selected saved data",
                    version,
                    version + 1,
                )
                return data

        return working_data

    # -------------------------------------------------
    # Build
    # -------------------------------------------------

    def _build_nodes(self, data: dict[str, Any]) -> None:
        for node in self.engine.metadata.nodes_by_schema.values():
            saved_data = None

            if self.engine.metadata.is_persistent(node):
                saved_data = data.get(node.schema.name)

            node.build(saved_data, recover=self._recovering)

    # -------------------------------------------------
    # Foreign-key validation
    # -------------------------------------------------

    def _validate_foreign_keys(self) -> None:
        while True:
            changed = False

            for node in self.engine.metadata.nodes_by_schema.values():
                if isinstance(node.schema, ManySchema):
                    changed |= self._validate_many(node)
                else:
                    changed |= self._validate_single(node)

            if not changed:
                return

    def _validate_many(self, node: Node) -> bool:
        invalid: list[tuple[str, ForeignKeyNotFoundError]] = []

        for row_id, row in node.value.items():
            if not isinstance(row, dict):
                continue

            try:
                self._validate_row(node, row_id, row)
            except ForeignKeyNotFoundError as exc:
                if not self._recovering:
                    raise

                invalid.append((row_id, exc))

        for row_id, error in invalid:
            logger.error(
                "Removing persisted row %s[%s]: %s",
                node.schema.name,
                row_id,
                error,
            )

            node.remove(row_id)

        return bool(invalid)

    def _validate_single(self, node: Node) -> bool:
        try:
            self._validate_row(node, None, node.value)
        except ForeignKeyNotFoundError as exc:
            if not self._recovering:
                raise

            logger.error(
                "Resetting persisted schema %s: %s", node.schema.name, exc
            )

            node.build(None, recover=False)
            self._validate_row(node, None, node.value)

            return True

        return False

    def _validate_row(
        self, node: Node, row_id: str | None, row: dict[str, Any]
    ) -> None:
        foreign_keys = self.engine.metadata.foreign_keys_of(node.schema.name)

        for foreign_key in foreign_keys:
            foreign_id = row.get(foreign_key.field)

            if foreign_id is None:
                continue

            reference_node = self.engine.get_node_by_schema(
                foreign_key.reference
            )
            reference_id = str(foreign_id)

            if reference_node.exists(reference_id):
                continue

            location = node.schema.name

            if row_id is not None:
                location += f"[{row_id}]"

            raise ForeignKeyNotFoundError(
                f"Foreign-key constraint violated: "
                f"{location}.{foreign_key.field} "
                f"references missing {foreign_key.reference}[{foreign_id}]",
                detail=ConstraintErrorDetail(
                    schema=node.schema.name,
                    fields=(foreign_key.field,),
                    value=foreign_id,
                    row_id=row_id,
                    reference_schema=foreign_key.reference,
                    reference_field=foreign_key.field,
                ),
            )

    # -------------------------------------------------
    # Save
    # -------------------------------------------------

    def _save_nodes(self) -> None:
        nodes = sorted(
            self.engine.metadata.persistent_nodes,
            key=lambda node: node.schema.name,
        )

        for node in nodes:
            expected = {
                "metadata": {
                    "store_version": self.engine.config.store_version
                },
                "data": node.value,
            }
            saved = self._raw_store.get(node.schema.name)

            # JSON comparison also distinguishes recovered bool/int values.
            if json.dumps(saved) == json.dumps(expected):
                continue

            self.engine._storage.write(
                node.schema.name,
                node.value,
                store_version=self.engine.config.store_version,
            )

    def _cleanup_store(self) -> None:
        active = {
            node.schema.name for node in self.engine.metadata.persistent_nodes
        }
        obsolete = self._raw_store.keys() - active
        storage = self.engine._storage
        removed = False

        with storage._lock:
            try:
                for schema_name in sorted(obsolete):
                    for path in (
                        storage._file_path(schema_name),
                        storage._temp_path(schema_name),
                    ):
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            continue

                        removed = True
                        logger.info("Removed obsolete Store snapshot %s", path)
            finally:
                if removed:
                    storage._fsync_directory()

    # -------------------------------------------------
    # Erase recovery
    # -------------------------------------------------

    def _erase_and_boot_empty(self) -> None:
        for node in self.engine.metadata.nodes_by_schema.values():
            node.build(None, recover=False)

        self._validate_foreign_keys()
