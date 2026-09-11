# src/kivyfn/store/query/shape.py
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kivyfn.store.query import Query
from kivyfn.store.query.query import JoinKind
from kivyfn.store.schema import ManySchema, Schema
from kivyfn.store.schema.fields import BaseField, ForeignKeyField


SchemaResolver = Callable[[str], Schema]


@dataclass(frozen=True, slots=True)
class QueryJoinShape:
    """One resolved foreign-key relationship in a Query shape."""

    owner_path: tuple[str, ...]
    relation_path: tuple[str, ...]
    field_name: str
    output_name: str
    output_prefix: str
    target_schema: str
    kind: JoinKind


@dataclass(frozen=True, slots=True)
class QueryFieldShape:
    """One flat output field exposed by a Query row."""

    name: str
    schema: str
    field: str
    relation_path: tuple[str, ...] = ()
    nullable: bool = False

    @property
    def joined(self) -> bool:
        return bool(self.relation_path)


@dataclass(frozen=True, slots=True)
class QueryShape:
    """Address-free schema-derived shape shared by equivalent outputs."""

    root_schema: str
    joins: tuple[QueryJoinShape, ...]
    fields: tuple[QueryFieldShape, ...]
    live_compatible: bool

    @classmethod
    def build(
        cls,
        query: Query[Any],
        root: ManySchema,
        resolve_schema: SchemaResolver,
    ) -> QueryShape:
        joins: list[QueryJoinShape] = []
        fields = _root_fields(root)

        field_names = {field.name for field in fields}

        relations: dict[tuple[str, ...], Schema] = {(): root}

        for join in query.joins:
            requested = join.path.parts
            owner_path = requested[:-1]

            owner = relations.get(owner_path)

            if owner is None:
                raise ValueError(
                    "Query joins must declare their parent relationship "
                    f"first: {join.path!s}"
                )

            field_name, relation = _foreign_key(owner, requested[-1])

            target = resolve_schema(relation.schema)

            if not isinstance(target, ManySchema):
                raise ValueError(
                    "Query foreign-key targets must use a ManySchema"
                )

            output_name = join.alias or relation.relation_name(field_name)
            relation_path = (*owner_path, output_name)

            if relation_path in relations:
                raise ValueError(
                    "Query join relationship already exists: "
                    f"{'.'.join(relation_path)}"
                )

            output_prefix = "_".join(relation_path)

            shape = QueryJoinShape(
                owner_path=owner_path,
                relation_path=relation_path,
                field_name=field_name,
                output_name=output_name,
                output_prefix=output_prefix,
                target_schema=target.name,
                kind=join.kind,
            )

            joined_fields = _joined_fields(shape, target)

            for joined_field in joined_fields:
                if joined_field.name in field_names:
                    raise ValueError(
                        "Query output field already exists: "
                        f"{joined_field.name!r}"
                    )

                field_names.add(joined_field.name)

            joins.append(shape)
            fields.extend(joined_fields)
            relations[relation_path] = target

        return cls(
            root_schema=root.name,
            joins=tuple(joins),
            fields=tuple(fields),
            live_compatible=True,
        )

    def field(self, name: str) -> QueryFieldShape:
        for field in self.fields:
            if field.name == name:
                return field

        raise KeyError(name)


def query_field_metadata(
    shape: QueryShape, resolve_schema: SchemaResolver
) -> dict[str, BaseField[Any]]:
    """Return Store field metadata for every flat Query output field."""
    result: dict[str, BaseField[Any]] = {}

    for field in shape.fields:
        schema = resolve_schema(field.schema)
        result[field.name] = schema.get_field(field.field)

    return result


def _root_fields(schema: ManySchema) -> list[QueryFieldShape]:
    return [
        QueryFieldShape(name=name, schema=schema.name, field=name)
        for name in schema.fields()
    ]


def _joined_fields(
    join: QueryJoinShape, schema: ManySchema
) -> list[QueryFieldShape]:
    nullable = join.kind is JoinKind.LEFT

    return [
        QueryFieldShape(
            name=f"{join.output_prefix}_{field_name}",
            schema=schema.name,
            field=field_name,
            relation_path=join.relation_path,
            nullable=nullable,
        )
        for field_name in schema.fields()
    ]


def _foreign_key(
    schema: Schema, relation_name: str
) -> tuple[str, ForeignKeyField]:
    candidates: list[tuple[str, ForeignKeyField]] = []

    for field_name, field in schema.foreign_key_fields().items():
        if field_name == relation_name:
            candidates.append((field_name, field))
            continue

        if field.relation_name(field_name) == relation_name:
            candidates.append((field_name, field))

    if not candidates:
        raise ValueError(
            f"Query join relation {relation_name!r} does not exist "
            f"on {schema.name}"
        )

    if len(candidates) > 1:
        raise ValueError(
            f"Query join relation {relation_name!r} is ambiguous "
            f"on {schema.name}"
        )

    return candidates[0]
