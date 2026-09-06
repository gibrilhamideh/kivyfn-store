# kivyfn/store/core/boot.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kivyfn.logging import get_logger
from kivyfn.store.builder.policy import BootPolicy
from kivyfn.store.error import (
    ConstraintErrorDetail,
    ForeignKeyNotFoundError,
)
from kivyfn.store.schema import (
    ManySchema,
    SingleSchema,
)


if TYPE_CHECKING:
    from kivyfn.store.core.engine import Engine
    from kivyfn.store.node import Node


logger = get_logger(__name__)


class StoreBoot:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def boot(self) -> None:
        try:
            raw_store = self._load_store()
            raw_data = self._extract_data(raw_store)
            migrated_data = self._migrate(
                raw_data,
                raw_store,
            )

            self._build_nodes(
                migrated_data,
            )
            self._validate_foreign_keys()
            self._save_nodes()

        except Exception:
            if self.engine.config.boot_policy is not BootPolicy.ERASE:
                raise

            logger.exception(
                "Store boot could not be recovered; "
                "erasing the entire persistent store"
            )

            self._erase_and_boot_empty()

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

    def _load_store(
        self,
    ) -> dict[str, dict[str, Any] | None]:
        schema_names = [
            node.schema.name
            for node in self.engine.metadata.persistent_nodes
        ]

        return self.engine._storage.load_store(
            schema_names,
        )

    def _extract_data(
        self,
        raw_store: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        data: dict[str, Any] = {}

        for node in self.engine.metadata.persistent_nodes:
            schema_name = node.schema.name
            payload = raw_store.get(
                schema_name,
            )

            if payload is None:
                data[schema_name] = None
                continue

            if not isinstance(
                payload,
                dict,
            ):
                if not self._recovering:
                    raise TypeError(
                        (
                            f"Persisted schema {schema_name!r} "
                            "must contain a dictionary payload"
                        )
                    )

                logger.error(
                    (
                        "Resetting persisted schema %s: "
                        "expected dictionary payload, got %s"
                    ),
                    schema_name,
                    type(payload).__name__,
                )

                data[schema_name] = None
                continue

            data[schema_name] = payload.get(
                "data",
            )

        return data

    # -------------------------------------------------
    # Migration
    # -------------------------------------------------

    def _migrate(
        self,
        data: dict[str, Any],
        raw_store: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        current_version = self._lowest_store_version(
            raw_store,
        )
        target_version = self.engine.config.store_version

        if current_version > target_version:
            raise RuntimeError(
                (
                    f"Storage version {current_version} is newer than "
                    f"store version {target_version}"
                )
            )

        while current_version < target_version:
            index = current_version - 1

            try:
                migration = self.engine.config.migrations[
                    index
                ]
            except IndexError:
                raise RuntimeError(
                    (
                        "Missing migration for store version "
                        f"{current_version} -> "
                        f"{current_version + 1}"
                    )
                ) from None

            data = migration.run(
                data,
            )
            current_version += 1

        return data

    def _lowest_store_version(
        self,
        raw_store: dict[str, dict[str, Any] | None],
    ) -> int:
        versions: list[int] = []

        for payload in raw_store.values():
            if not isinstance(
                payload,
                dict,
            ):
                continue

            metadata = payload.get(
                "metadata",
                {},
            )

            if not isinstance(
                metadata,
                dict,
            ):
                continue

            version = metadata.get(
                "store_version",
                1,
            )

            if not isinstance(
                version,
                int,
            ) or version < 1:
                raise RuntimeError(
                    f"Invalid store_version: {version!r}"
                )

            versions.append(
                version,
            )

        if not versions:
            return self.engine.config.store_version

        return min(
            versions,
        )

    # -------------------------------------------------
    # Build
    # -------------------------------------------------

    def _build_nodes(
        self,
        data: dict[str, Any],
    ) -> None:
        for node in self.engine.metadata.persistent_nodes:
            node_data = data.get(
                node.schema.name,
            )

            node.build(
                node_data,
                recover=self._recovering,
            )

    # -------------------------------------------------
    # Foreign-key validation
    # -------------------------------------------------

    def _validate_foreign_keys(self) -> None:
        while True:
            changed = False

            for node in self.engine.metadata.persistent_nodes:
                if isinstance(
                    node.schema,
                    ManySchema,
                ):
                    changed |= self._validate_many_foreign_keys(
                        node,
                    )
                    continue

                if isinstance(
                    node.schema,
                    SingleSchema,
                ):
                    changed |= self._validate_single_foreign_keys(
                        node,
                    )
                    continue

                raise TypeError(
                    (
                        f"Schema {node.schema.name!r} must inherit "
                        "SingleSchema or ManySchema"
                    )
                )

            if not changed:
                return

    def _validate_many_foreign_keys(
        self,
        node: Node,
    ) -> bool:
        invalid_rows: list[
            tuple[str, ForeignKeyNotFoundError]
        ] = []

        for row_id, row in node.value.items():
            try:
                self._validate_row_foreign_keys(
                    node,
                    row_id,
                    row,
                )
            except ForeignKeyNotFoundError as error:
                if not self._recovering:
                    raise

                invalid_rows.append(
                    (
                        row_id,
                        error,
                    )
                )

        for row_id, error in invalid_rows:
            logger.error(
                "Removing persisted row %s[%s]: %s",
                node.schema.name,
                row_id,
                error,
            )

            node.remove(
                row_id,
            )

        return bool(
            invalid_rows,
        )

    def _validate_single_foreign_keys(
        self,
        node: Node,
    ) -> bool:
        try:
            self._validate_row_foreign_keys(
                node,
                None,
                node.value,
            )
        except ForeignKeyNotFoundError as error:
            if not self._recovering:
                raise

            logger.error(
                "Resetting persisted schema %s: %s",
                node.schema.name,
                error,
            )

            node.build(
                None,
                recover=False,
            )

            self._validate_row_foreign_keys(
                node,
                None,
                node.value,
            )

            return True

        return False

    def _validate_row_foreign_keys(
        self,
        node: Node,
        row_id: str | None,
        row: dict[str, Any],
    ) -> None:
        foreign_keys = node.schema.foreign_key_fields()

        for field_name, field in foreign_keys.items():
            foreign_id = row.get(
                field_name,
            )

            if foreign_id is None:
                continue

            reference_node = self.engine.get_node_by_schema(
                field.schema,
            )

            if not isinstance(
                reference_node.schema,
                ManySchema,
            ):
                raise ValueError(
                    (
                        "Foreign-key target must be a many schema: "
                        f"{field.schema}"
                    )
                )

            reference_id = str(
                foreign_id,
            )

            if reference_node.exists(
                reference_id,
            ):
                continue

            location = node.schema.name

            if row_id is not None:
                location += f"[{row_id}]"

            raise ForeignKeyNotFoundError(
                (
                    "Foreign-key constraint violated: "
                    f"{location}.{field_name} references missing "
                    f"{field.schema}[{foreign_id}]"
                ),
                detail=ConstraintErrorDetail(
                    schema=node.schema.name,
                    fields=(
                        field_name,
                    ),
                    value=foreign_id,
                    row_id=row_id,
                    reference_schema=field.schema,
                ),
            )

    # -------------------------------------------------
    # Save
    # -------------------------------------------------

    def _save_nodes(self) -> None:
        for node in self.engine.metadata.persistent_nodes:
            self.engine._storage.write(
                node.schema.name,
                node.value,
                store_version=(
                    self.engine.config.store_version
                ),
            )

    # -------------------------------------------------
    # Erase recovery
    # -------------------------------------------------

    def _erase_and_boot_empty(self) -> None:
        for node in self.engine.metadata.persistent_nodes:
            node.build(
                None,
                recover=False,
            )

        self._save_nodes()
