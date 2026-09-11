# src/kivyfn/store/core/metadata.py
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from kivyfn.store.address import Address
from kivyfn.store.schema.policy import ForeignKeyPolicy
from kivyfn.store.storage import parse_persist_interval


if TYPE_CHECKING:
    from kivyfn.store.node import Node


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """Resolved foreign-key metadata for one schema field."""

    schema: str
    field: str
    reference: str
    on_delete: ForeignKeyPolicy


@dataclass(frozen=True, slots=True)
class EngineMetadata:
    """Immutable metadata derived from the Store node tree."""

    nodes_by_address: Mapping[Address, Node]
    nodes_by_schema: Mapping[str, Node]
    persistent_nodes: frozenset[Node]
    persistence_intervals: Mapping[Node, float]
    _foreign_keys_by_schema: Mapping[
        str,
        tuple[ForeignKey, ...],
    ]
    _referenced_by_schema: Mapping[
        str,
        tuple[ForeignKey, ...],
    ]

    @classmethod
    def build(
        cls,
        root: Node,
    ) -> EngineMetadata:
        nodes_by_address: dict[
            Address,
            Node,
        ] = {}

        nodes_by_schema: dict[
            str,
            Node,
        ] = {}

        persistent_nodes: set[Node] = set()

        persistence_intervals: dict[
            Node,
            float,
        ] = {}

        _register_nodes(
            root,
            nodes_by_address=nodes_by_address,
            nodes_by_schema=nodes_by_schema,
            persistent_nodes=persistent_nodes,
            persistence_intervals=persistence_intervals,
        )

        (
            foreign_keys_by_schema,
            referenced_by_schema,
        ) = _build_foreign_keys(
            nodes_by_schema,
        )

        _validate_delayed_persistence(
            persistence_intervals,
            foreign_keys_by_schema,
        )

        return cls(
            nodes_by_address=MappingProxyType(
                nodes_by_address
            ),
            nodes_by_schema=MappingProxyType(
                nodes_by_schema
            ),
            persistent_nodes=frozenset(
                persistent_nodes
            ),
            persistence_intervals=MappingProxyType(
                persistence_intervals
            ),
            _foreign_keys_by_schema=MappingProxyType(
                foreign_keys_by_schema
            ),
            _referenced_by_schema=MappingProxyType(
                referenced_by_schema
            ),
        )

    def get_node_by_address(
        self,
        address: Address,
    ) -> Node:
        try:
            return self.nodes_by_address[
                address
            ]
        except KeyError:
            raise RuntimeError(
                f"Node address not found: {address}"
            ) from None

    def get_node_by_schema(
        self,
        schema_name: str,
    ) -> Node:
        try:
            return self.nodes_by_schema[
                schema_name
            ]
        except KeyError:
            raise RuntimeError(
                "No node found for schema "
                f"{schema_name!r}"
            ) from None

    def foreign_keys_of(
        self,
        schema_name: str,
    ) -> tuple[ForeignKey, ...]:
        return self._foreign_keys_by_schema.get(
            schema_name,
            (),
        )

    def referenced_by(
        self,
        schema_name: str,
    ) -> tuple[ForeignKey, ...]:
        return self._referenced_by_schema.get(
            schema_name,
            (),
        )

    def is_persistent(
        self,
        node: Node,
    ) -> bool:
        return (
            node
            in self.persistent_nodes
        )

    def persistence_interval(
        self,
        node: Node,
    ) -> float | None:
        return self.persistence_intervals.get(
            node
        )


def _register_nodes(
    node: Node,
    *,
    nodes_by_address: dict[Address, Node],
    nodes_by_schema: dict[str, Node],
    persistent_nodes: set[Node],
    persistence_intervals: dict[Node, float],
) -> None:
    address = node.address
    schema = node.schema
    schema_name = schema.name

    if address in nodes_by_address:
        raise RuntimeError(
            f"Duplicate node address: {address}"
        )

    if schema_name in nodes_by_schema:
        raise RuntimeError(
            f"Schema {schema_name!r} is attached "
            "to multiple nodes"
        )

    nodes_by_address[address] = node
    nodes_by_schema[schema_name] = node

    interval = parse_persist_interval(
        schema.persist_interval
    )

    if (
        interval is not None
        and not schema.persistent
    ):
        raise ValueError(
            f"Schema {schema_name!r} defines "
            "persist_interval but persistent is False"
        )

    if schema.persistent:
        persistent_nodes.add(
            node
        )

    if interval is not None:
        persistence_intervals[
            node
        ] = interval

    for child in node.children.values():
        _register_nodes(
            child,
            nodes_by_address=nodes_by_address,
            nodes_by_schema=nodes_by_schema,
            persistent_nodes=persistent_nodes,
            persistence_intervals=persistence_intervals,
        )


def _build_foreign_keys(
    nodes_by_schema: Mapping[str, Node],
) -> tuple[
    dict[str, tuple[ForeignKey, ...]],
    dict[str, tuple[ForeignKey, ...]],
]:
    by_schema: defaultdict[
        str,
        list[ForeignKey],
    ] = defaultdict(list)

    referenced_by: defaultdict[
        str,
        list[ForeignKey],
    ] = defaultdict(list)

    for (
        schema_name,
        node,
    ) in nodes_by_schema.items():
        for (
            field_name,
            field,
        ) in node.schema.foreign_key_fields().items():
            reference = field.schema

            if reference not in nodes_by_schema:
                raise RuntimeError(
                    "Foreign key reference schema "
                    f"{reference!r} is not registered"
                )

            foreign_key = ForeignKey(
                schema=schema_name,
                field=field_name,
                reference=reference,
                on_delete=field.on_delete,
            )

            by_schema[
                schema_name
            ].append(
                foreign_key
            )

            referenced_by[
                reference
            ].append(
                foreign_key
            )

    return (
        {
            schema: tuple(
                foreign_keys
            )
            for (
                schema,
                foreign_keys,
            ) in by_schema.items()
        },
        {
            schema: tuple(
                foreign_keys
            )
            for (
                schema,
                foreign_keys,
            ) in referenced_by.items()
        },
    )


def _validate_delayed_persistence(
    persistence_intervals: Mapping[
        Node,
        float,
    ],
    foreign_keys_by_schema: Mapping[
        str,
        tuple[ForeignKey, ...],
    ],
) -> None:
    delayed = {
        node.schema.name
        for node in persistence_intervals
    }

    for foreign_keys in (
        foreign_keys_by_schema.values()
    ):
        for foreign_key in foreign_keys:
            if (
                foreign_key.schema not in delayed
                and foreign_key.reference not in delayed
            ):
                continue

            raise ValueError(
                "Delayed-persistence schemas cannot "
                "participate in foreign-key relationships: "
                f"{foreign_key.schema}."
                f"{foreign_key.field} -> "
                f"{foreign_key.reference}"
            )
