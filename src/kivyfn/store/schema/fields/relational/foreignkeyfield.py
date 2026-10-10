from __future__ import annotations

from typing import Any

from kivyfn.store.error import FieldErrorDetail, FieldValidationError
from kivyfn.store.query.expression import Expression
from kivyfn.store.schema.fields.base import BaseField
from kivyfn.store.schema.policy import ForeignKeyPolicy


class ForeignKeyField(BaseField[str | None]):
    """A reference, optionally ensuring one child per qualifying parent.

    ``ensure`` requires a single-field unique constraint on this field.
    ``ensure_where`` is a query expression over the referenced parent row.
    """

    def __init__(
        self,
        schema: str,
        *,
        relation: str | None = None,
        on_delete: ForeignKeyPolicy = ForeignKeyPolicy.RESTRICT,
        ensure: bool = False,
        ensure_where: Expression | None = None,
    ) -> None:
        super().__init__()

        if type(ensure) is not bool:
            raise TypeError("Foreign-key ensure must be a bool")
        if ensure_where is not None:
            if not isinstance(ensure_where, Expression):
                raise TypeError("ensure_where must be a query expression")
            if not ensure:
                raise ValueError("ensure_where requires ensure=True")

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
        self.ensure = ensure
        self.ensure_where = ensure_where

    @property
    def metadata(self) -> dict[str, Any]:
        from kivyfn.store.codec.query import expression_to_payload

        return {
            "schema": self.schema,
            "relation": self.relation_name(),
            "on_delete": self.on_delete.value,
            "allow_none": True,
            "ensure": self.ensure,
            "ensure_where": (
                expression_to_payload(self.ensure_where)
                if self.ensure_where is not None
                else None
            ),
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
