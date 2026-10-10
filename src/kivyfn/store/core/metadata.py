# src/kivyfn/store/core/metadata.py
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from kivyfn.store.address import Address
from kivyfn.store.error import NodeErrorDetail, NodeNotFoundError
from kivyfn.store.query.expression import Expression, iter_field_paths
from kivyfn.store.schema import ManySchema, SingleSchema
from kivyfn.store.schema.policy import ForeignKeyPolicy

if TYPE_CHECKING:
    from kivyfn.store.node import Node


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """Resolved foreign-key metadata for one schema field."""

    schema: str
    field: str
    reference: str
    on_delete: ForeignKeyPolicy
    ensure: bool
    ensure_where: Expression | None


@dataclass(frozen=True, slots=True)
class EngineMetadata:
    """Immutable metadata derived from the Store node tree."""

    nodes_by_address: Mapping[Address, Node]
    nodes_by_schema: Mapping[str, Node]
    persistent_nodes: frozenset[Node]
    ensured_foreign_keys: tuple[ForeignKey, ...]
    _foreign_keys_by_schema: Mapping[str, tuple[ForeignKey, ...]]
    _referenced_by_schema: Mapping[str, tuple[ForeignKey, ...]]

    @classmethod
    def build(cls, root: Node) -> EngineMetadata:
        nodes_by_address: dict[Address, Node] = {}

        nodes_by_schema: dict[str, Node] = {}

        persistent_nodes: set[Node] = set()

        _register_nodes(
            root,
            nodes_by_address=nodes_by_address,
            nodes_by_schema=nodes_by_schema,
            persistent_nodes=persistent_nodes,
        )

        (foreign_keys_by_schema, referenced_by_schema) = _build_foreign_keys(
            nodes_by_schema
        )

        return cls(
            nodes_by_address=MappingProxyType(nodes_by_address),
            nodes_by_schema=MappingProxyType(nodes_by_schema),
            persistent_nodes=frozenset(persistent_nodes),
            ensured_foreign_keys=_order_ensures(foreign_keys_by_schema),
            _foreign_keys_by_schema=MappingProxyType(foreign_keys_by_schema),
            _referenced_by_schema=MappingProxyType(referenced_by_schema),
        )

    def get_node_by_address(self, address: Address) -> Node:
        try:
            return self.nodes_by_address[address]
        except KeyError:
            raise NodeNotFoundError(
                f"Node address not found: {address}",
                detail=NodeErrorDetail(address=address.parts),
            ) from None

    def get_node_by_schema(self, schema_name: str) -> Node:
        try:
            return self.nodes_by_schema[schema_name]
        except KeyError:
            raise NodeNotFoundError(
                f"No node found for schema {schema_name!r}",
                detail=NodeErrorDetail(schema=schema_name),
            ) from None

    def foreign_keys_of(self, schema_name: str) -> tuple[ForeignKey, ...]:
        return self._foreign_keys_by_schema.get(schema_name, ())

    def referenced_by(self, schema_name: str) -> tuple[ForeignKey, ...]:
        return self._referenced_by_schema.get(schema_name, ())

    def is_persistent(self, node: Node) -> bool:
        return node in self.persistent_nodes


def _register_nodes(
    node: Node,
    *,
    nodes_by_address: dict[Address, Node],
    nodes_by_schema: dict[str, Node],
    persistent_nodes: set[Node],
) -> None:
    address = node.address
    schema = node.schema
    schema_name = schema.name

    if address in nodes_by_address:
        raise RuntimeError(f"Duplicate node address: {address}")

    if schema_name in nodes_by_schema:
        raise RuntimeError(
            f"Schema {schema_name!r} is attached to multiple nodes"
        )

    nodes_by_address[address] = node
    nodes_by_schema[schema_name] = node

    if schema.persistent:
        persistent_nodes.add(node)

    for child in node.children.values():
        _register_nodes(
            child,
            nodes_by_address=nodes_by_address,
            nodes_by_schema=nodes_by_schema,
            persistent_nodes=persistent_nodes,
        )


def _build_foreign_keys(
    nodes_by_schema: Mapping[str, Node],
) -> tuple[
    dict[str, tuple[ForeignKey, ...]], dict[str, tuple[ForeignKey, ...]]
]:
    by_schema: defaultdict[str, list[ForeignKey]] = defaultdict(list)

    referenced_by: defaultdict[str, list[ForeignKey]] = defaultdict(list)

    for schema_name, node in nodes_by_schema.items():
        for field_name, field in node.schema.foreign_key_fields().items():
            reference = field.schema

            if reference not in nodes_by_schema:
                raise RuntimeError(
                    "Foreign key reference schema "
                    f"{reference!r} is not registered"
                )

            if not isinstance(nodes_by_schema[reference].schema, ManySchema):
                raise ValueError(
                    f"Foreign key {schema_name}.{field_name} must reference "
                    "a ManySchema"
                )
            if (
                isinstance(node.schema, SingleSchema)
                and field.on_delete is ForeignKeyPolicy.CASCADE
            ):
                raise ValueError(
                    f"Foreign key {schema_name}.{field_name}: "
                    "CASCADE requires "
                    "a ManySchema source; singleton records cannot be deleted"
                )

            if field.ensure:
                if not isinstance(node.schema, ManySchema):
                    raise ValueError(
                        f"Foreign key {schema_name}.{field_name}: "
                        "ensure requires a ManySchema source"
                    )
                if (field_name,) not in node.schema.unique_keys():
                    raise ValueError(
                        f"Foreign key {schema_name}.{field_name}: ensure "
                        "requires a single-field unique constraint"
                    )
                if field.ensure_where is not None:
                    for path in iter_field_paths(field.ensure_where):
                        if len(path.parts) != 1:
                            raise ValueError(
                                "ensure_where supports direct parent fields "
                                f"only: {schema_name}.{field_name}"
                            )
                        nodes_by_schema[reference].schema.get_field(
                            path.parts[0]
                        )

            foreign_key = ForeignKey(
                schema=schema_name,
                field=field_name,
                reference=reference,
                on_delete=field.on_delete,
                ensure=field.ensure,
                ensure_where=field.ensure_where,
            )

            by_schema[schema_name].append(foreign_key)

            referenced_by[reference].append(foreign_key)

    return (
        {
            schema: tuple(foreign_keys)
            for (schema, foreign_keys) in by_schema.items()
        },
        {
            schema: tuple(foreign_keys)
            for (schema, foreign_keys) in referenced_by.items()
        },
    )


def _order_ensures(
    foreign_keys: Mapping[str, tuple[ForeignKey, ...]],
) -> tuple[ForeignKey, ...]:
    """Order parent-to-child creation and reject unbounded ensure cycles."""
    dependents: defaultdict[str, list[ForeignKey]] = defaultdict(list)
    for fields in foreign_keys.values():
        for field in fields:
            if field.ensure:
                dependents[field.reference].append(field)

    ordered: list[ForeignKey] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(schema: str) -> None:
        if schema in visiting:
            raise ValueError(f"Cyclic ensure relationships include {schema}")
        if schema in visited:
            return
        visiting.add(schema)
        for field in dependents.get(schema, ()):
            visit(field.schema)
            ordered.append(field)
        visiting.remove(schema)
        visited.add(schema)

    for schema in dependents:
        visit(schema)
    return tuple(reversed(ordered))
