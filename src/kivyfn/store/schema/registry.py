from __future__ import annotations

from dataclasses import dataclass

from kivyfn.store.constants import Policy


@dataclass(frozen=True)
class ForeignKey:
    schema: str
    field: str
    reference: str
    on_delete: Policy

    def __repr__(self) -> str:
        return (
            f"ForeignKey("
            f"{self.schema}.{self.field} -> {self.reference}, "
            f"on_delete={self.on_delete.name})"
        )


class ForeignKeyRegistry:
    def __init__(self) -> None:
        self._forward: dict[tuple[str, str], ForeignKey] = {}
        self._reverse: dict[str, list[ForeignKey]] = {}
        self._by_source: dict[str, list[ForeignKey]] = {}

    def register(
        self,
        *,
        schema: str,
        field: str,
        reference: str,
        on_delete: Policy,
    ) -> None:
        key = (schema, field)

        if key in self._forward:
            raise ValueError(f"Foreign key already registered for {schema}.{field}")

        fk = ForeignKey(
            schema=schema,
            field=field,
            reference=reference,
            on_delete=on_delete,
        )

        self._forward[key] = fk
        self._reverse.setdefault(reference, []).append(fk)
        self._by_source.setdefault(schema, []).append(fk)

    def get(self, schema: str, field: str) -> ForeignKey | None:
        return self._forward.get((schema, field))

    def referenced_by(self, schema: str) -> list[ForeignKey]:
        return self._reverse.get(schema, []).copy()

    def foreign_keys_of(self, schema: str) -> list[ForeignKey]:
        return self._by_source.get(schema, []).copy()

    def all(self) -> list[ForeignKey]:
        return list(self._forward.values())

    def __repr__(self) -> str:
        if not self._forward:
            return "ForeignKeyRegistry(empty)"

        lines = ["ForeignKeyRegistry:"]

        for schema, fks in self._by_source.items():
            lines.append(f"  {schema}:")
            for fk in fks:
                lines.append(f"    - {fk.field} -> {fk.reference} ({fk.on_delete.name})")

        return "\n".join(lines)
