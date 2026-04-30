from __future__ import annotations
from dataclasses import dataclass

from ..constants import Policy


@dataclass(frozen=True)
class ForeignKey:
    schema: str
    field: str
    reference: str
    on_delete: Policy


class ForeignKeyRegistry:
    """
    Registry storing foreign key relationships.

    Stores:
        - Forward mapping: (source_schema, field) → ForeignKey
        - Reverse mapping: target_schema → list[ForeignKey]

    Uses schema names (strings) only.
    Resolution happens via SchemaRegistry when needed.
    """

    _forward: dict[tuple[str, str], ForeignKey] = {}
    _reverse: dict[str, list[ForeignKey]] = {}
    _by_source: dict[str, list[ForeignKey]] = {}

    # ------------------------------------------------
    # Registration
    # ------------------------------------------------

    @classmethod
    def register(
        cls,
        *,
        schema: str,
        field: str,
        reference: str,
        on_delete: Policy,
    ) -> None:
        key = (schema, field)

        if key in cls._forward:
            raise RuntimeError(f"Foreign key already registered for {schema}.{field}")

        fk = ForeignKey(
            schema=schema,
            field=field,
            reference=reference,
            on_delete=on_delete,
        )

        cls._forward[key] = fk
        cls._reverse.setdefault(reference, []).append(fk)
        cls._by_source.setdefault(schema, []).append(fk)

    # ------------------------------------------------
    # Forward lookup
    # ------------------------------------------------

    @classmethod
    def get(
        cls,
        schema: str,
        field: str,
    ) -> ForeignKey | None:
        return cls._forward.get((schema, field))

    # ------------------------------------------------
    # Reverse lookup
    # ------------------------------------------------
    @classmethod
    def referenced_by(
        cls,
        schema: str,
    ) -> list[ForeignKey]:
        """
        Return all foreign keys (schemas) referencing the given schema.

        """
        return cls._reverse.get(schema, []).copy()

    @classmethod
    def foreign_keys_of(cls, schema: str) -> list[ForeignKey]:
        """
        Return all foreign keys defined on the given schema.

        """
        return cls._by_source.get(schema, []).copy()

    # ------------------------------------------------
    # Introspection
    # ------------------------------------------------

    @classmethod
    def all(cls) -> list[ForeignKey]:
        return list(cls._forward.values())
