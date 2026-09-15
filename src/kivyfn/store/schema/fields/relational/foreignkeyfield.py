from __future__ import annotations

from typing import Any

from kivyfn.store.error import FieldErrorDetail, FieldValidationError
from kivyfn.store.schema.fields.base import BaseField
from kivyfn.store.schema.policy import ForeignKeyPolicy


class ForeignKeyField(BaseField[str | None]):
    """An optional row ID; Store constraints validate its referenced row."""

    def __init__(
        self,
        schema: str,
        *,
        relation: str | None = None,
        on_delete: ForeignKeyPolicy = ForeignKeyPolicy.RESTRICT,
    ) -> None:
        super().__init__()

        if not isinstance(schema, str) or not schema:
            raise ValueError("Foreign-key schema must be a non-empty string")

        if relation is not None:
            if not isinstance(relation, str) or not relation:
                raise ValueError(
                    "Foreign-key relation must be a non-empty string"
                )

            if "." in relation:
                raise ValueError("Foreign-key relation cannot contain dots")

        self.schema = schema
        self.relation = relation
        self.on_delete = ForeignKeyPolicy(on_delete)

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "relation": self.relation_name(),
            "on_delete": self.on_delete.value,
            "allow_none": True,
        }

    def relation_name(self, field_name: str | None = None) -> str:
        if self.relation is not None:
            return self.relation

        name = field_name or self.name

        if name.endswith("_id"):
            return name[:-3]

        return name

    def challenge(self, value: Any) -> None:
        if value is None or isinstance(value, str) and value:
            return
        raise FieldValidationError(
            "Expected a non-empty row ID or None",
            detail=FieldErrorDetail(
                field=self.name,
                value=value,
                rule="type",
                expected="non-empty str or None",
            ),
        )
