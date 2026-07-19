from __future__ import annotations

from kivyfn.store.schema.fields.base import BaseField
from kivyfn.store.schema.policy import ForeignKeyPolicy


class ForeignKeyField(BaseField):
    def __init__(
        self,
        schema: str,
        *,
        on_delete: ForeignKeyPolicy = ForeignKeyPolicy.RESTRICT,
    ):
        super().__init__()
        self.schema: str = schema
        self.on_delete: ForeignKeyPolicy = on_delete
