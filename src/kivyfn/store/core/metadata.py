from __future__ import annotations

from typing import TYPE_CHECKING

from kivyfn.store.address import Address
from kivyfn.store.schema.registry import ForeignKey, ForeignKeyRegistry
from kivyfn.store.storage import parse_persist_interval


if TYPE_CHECKING:
    from kivyfn.store.node import Node
    from kivyfn.store.schema import Schema


class EngineMetadata:
    def __init__(self) -> None:
        self.nodes_by_address: dict[Address, Node] = {}
        self.nodes_by_schema: dict[str, Node] = {}
        self.schemas_by_name: dict[str, Schema] = {}

        self.persistent_nodes: set[Node] = set()
        self.persistence_intervals: dict[Node, float] = {}

        self.foreign_keys = ForeignKeyRegistry()

    @classmethod
    def build(cls, root: Node) -> EngineMetadata:
        metadata = cls()
        metadata._register_nodes(root)
        metadata._build_foreign_keys()
        metadata._validate_foreign_keys()
        metadata._validate_delayed_persistence()
        return metadata

    # -------------------------------------------------
    # Build steps
    # -------------------------------------------------

    def _register_nodes(self, node: Node) -> None:
        address = node.address
        schema = node.schema
        schema_name = schema.name

        if address in self.nodes_by_address:
            raise RuntimeError(f"Duplicate node address: {address}")

        if schema_name in self.nodes_by_schema:
            raise RuntimeError(
                f"Schema '{schema_name}' is attached to multiple nodes"
            )

        self.nodes_by_address[address] = node
        self.nodes_by_schema[schema_name] = node
        self.schemas_by_name[schema_name] = schema

        interval = parse_persist_interval(schema.persist_interval)

        if interval is not None and not schema.persistent:
            raise ValueError(
                f"Schema '{schema_name}' defines persist_interval but "
                "persistent is False"
            )

        if schema.persistent:
            self.persistent_nodes.add(node)

        if interval is not None:
            self.persistence_intervals[node] = interval

        for child in node.children.values():
            self._register_nodes(child)

    def _build_foreign_keys(self) -> None:
        for schema_name, schema in self.schemas_by_name.items():
            for field_name, field in schema.foreign_key_fields().items():
                self.foreign_keys.register(
                    schema=schema_name,
                    field=field_name,
                    reference=field.schema,
                    on_delete=field.on_delete,
                )

    def _validate_foreign_keys(self) -> None:
        for foreign_key in self.foreign_keys.all():
            if foreign_key.schema not in self.schemas_by_name:
                raise RuntimeError(
                    "Foreign key source schema "
                    f"'{foreign_key.schema}' is not registered in metadata"
                )

            if foreign_key.reference not in self.schemas_by_name:
                raise RuntimeError(
                    "Foreign key reference schema "
                    f"'{foreign_key.reference}' is not registered in metadata"
                )

    def _validate_delayed_persistence(self) -> None:
        delayed = {
            node.schema.name
            for node in self.persistence_intervals
        }

        for foreign_key in self.foreign_keys.all():
            if (
                foreign_key.schema not in delayed
                and foreign_key.reference not in delayed
            ):
                continue

            raise ValueError(
                "Delayed-persistence schemas cannot participate in "
                "foreign-key relationships: "
                f"{foreign_key.schema}.{foreign_key.field} -> "
                f"{foreign_key.reference}"
            )

    # -------------------------------------------------
    # Lookups
    # -------------------------------------------------

    def get_node_by_address(self, address: Address) -> Node:
        try:
            return self.nodes_by_address[address]
        except KeyError:
            raise RuntimeError(
                f"Node address not found: {address}"
            ) from None

    def get_node_by_schema(self, schema_name: str) -> Node:
        try:
            return self.nodes_by_schema[schema_name]
        except KeyError:
            raise RuntimeError(
                f"No node found for schema '{schema_name}'"
            ) from None

    def get_schema(self, schema_name: str) -> Schema:
        try:
            return self.schemas_by_name[schema_name]
        except KeyError:
            raise RuntimeError(
                f"Schema '{schema_name}' is not registered"
            ) from None

    def foreign_keys_of(self, schema_name: str) -> list[ForeignKey]:
        return self.foreign_keys.foreign_keys_of(schema_name)

    def referenced_by(self, schema_name: str) -> list[ForeignKey]:
        return self.foreign_keys.referenced_by(schema_name)

    def is_persistent(self, node: Node) -> bool:
        return node in self.persistent_nodes

    def persistence_interval(self, node: Node) -> float | None:
        return self.persistence_intervals.get(node)
