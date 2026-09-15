# tests/schemas/users.py
from kivyfn.store.operations import OpCode
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.context import TriggerContext
from kivyfn.store.schema.fields import (
    BooleanField,
    EnumField,
    NumericField,
    StringField,
)
from kivyfn.store.schema.trigger import trigger
from kivyfn.store.transaction import Transaction


class UserSchema(ManySchema):
    persistent = True

    uniques = [("email",)]

    username = StringField(default="")
    email = StringField(default="")
    age = NumericField(ge=0, le=130, default=0)
    active = BooleanField(default=True)
    role = EnumField(values=("user", "admin"), default="user")

    @trigger(OpCode.INSERT)
    def create_settings(
        self, transaction: Transaction, context: TriggerContext
    ) -> None:
        row_id = context.id

        if row_id is None:
            raise RuntimeError("User insert trigger requires a row ID")

        settings = context.engine.get_node_by_schema("UserSettingsSchema")

        transaction.insert(settings.address, {"user_id": row_id}, id=row_id)
