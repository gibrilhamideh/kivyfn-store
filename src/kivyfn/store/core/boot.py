from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.store.constants import ConstraintType
from kivyfn.store.errors.store import ConstraintError
from kivyfn.store.builder.policy import BootPolicy

if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


class StoreBoot:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def boot(self) -> None:
        try:
            raw_store = self._load_store()
            raw_data = self._extract_data(raw_store)
            migrated_data = self._migrate(raw_data, raw_store)

            self._build_nodes(migrated_data)
            self._validate_foreign_keys()
            self._save_nodes()

        except Exception:
            if self.engine.config.boot_policy is BootPolicy.RAISE:
                raise

            if self.engine.config.boot_policy is BootPolicy.ERASE:
                self._erase_and_boot_empty()
                return

            raise

    # -------------------------------------------------
    # Load
    # -------------------------------------------------

    def _load_store(self) -> dict[str, dict[str, Any] | None]:
        schema_names = [
            node.schema.name
            for node in self.engine.metadata.persistent_nodes
        ]

        return self.engine._storage.load_store(schema_names)

    def _extract_data(
        self,
        raw_store: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        data: dict[str, Any] = {}

        for node in self.engine.metadata.persistent_nodes:
            payload = raw_store.get(node.schema.name)

            if not isinstance(payload, dict):
                data[node.schema.name] = None
                continue

            data[node.schema.name] = payload.get("data")

        return data

    # -------------------------------------------------
    # Migration
    # -------------------------------------------------

    def _migrate(
        self,
        data: dict[str, Any],
        raw_store: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        current_version = self._lowest_store_version(raw_store)
        target_version = self.engine.config.store_version

        if current_version > target_version:
            raise RuntimeError(
                f"Storage version {current_version} is newer than "
                f"store version {target_version}"
            )

        while current_version < target_version:
            index = current_version - 1

            try:
                migration = self.engine.config.migrations[index]
            except IndexError:
                raise RuntimeError(
                    f"Missing migration for store version "
                    f"{current_version} -> {current_version + 1}"
                ) from None

            data = migration.run(data)
            current_version += 1

        return data

    def _lowest_store_version(
        self,
        raw_store: dict[str, dict[str, Any] | None],
    ) -> int:
        versions: list[int] = []

        for payload in raw_store.values():
            if not isinstance(payload, dict):
                continue

            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                continue

            version = metadata.get("store_version", 1)

            if not isinstance(version, int) or version < 1:
                raise RuntimeError(f"Invalid store_version: {version!r}")

            versions.append(version)

        if not versions:
            return self.engine.config.store_version

        return min(versions)

    # -------------------------------------------------
    # Build
    # -------------------------------------------------

    def _build_nodes(self, data: dict[str, Any]) -> None:
        for node in self.engine.metadata.persistent_nodes:
            node_data = data.get(node.schema.name)
            node.build(node_data)

    # -------------------------------------------------
    # Foreign Key Validation
    # -------------------------------------------------

    def _validate_foreign_keys(self) -> None:
        for node in self.engine.metadata.persistent_nodes:
            if node.schema.many:
                for row_id, row in node.value.items():
                    self._validate_row_foreign_keys(node, row_id, row)
            else:
                self._validate_row_foreign_keys(node, None, node.value)

    def _validate_row_foreign_keys(
        self,
        node: Node,
        row_id: str | None,
        row: dict[str, Any],
    ) -> None:
        for field_name, field in node.schema.foreign_key_fields().items():
            foreign_id = row.get(field_name)

            if foreign_id is None:
                continue

            ref_node = self.engine.get_node_by_schema(field.schema)

            if not ref_node.schema.many:
                raise ConstraintError(
                    f"Foreign key target must be a many schema: "
                    f"{field.schema}",
                    constraint_type=ConstraintType.FOREIGN_KEY,
                )

            if not ref_node.exists(str(foreign_id)):
                location = node.schema.name
                if row_id is not None:
                    location += f"[{row_id}]"

                raise ConstraintError(
                    f"Foreign key constraint violated: "
                    f"{location}.{field_name} references missing "
                    f"{field.schema}[{foreign_id}]",
                    constraint_type=ConstraintType.FOREIGN_KEY,
                )

    # -------------------------------------------------
    # Save
    # -------------------------------------------------

    def _save_nodes(self) -> None:
        for node in self.engine.metadata.persistent_nodes:
            self.engine._storage.write(
                node.schema.name,
                node.value,
                store_version=self.engine.config.store_version,
            )

    # -------------------------------------------------
    # Erase Recovery
    # -------------------------------------------------

    def _erase_and_boot_empty(self) -> None:
        for node in self.engine.metadata.persistent_nodes:
            node.build(None)

        self._save_nodes()
