# tests/schemas/settings.py
from kivyfn.store.schema import ManySchema
from kivyfn.store.schema.fields import BooleanField, EnumField, ForeignKeyField
from kivyfn.store.schema.policy import ForeignKeyPolicy


class UserSettingsSchema(ManySchema):
    persistent = True

    uniques = [
        ("user_id",),
    ]

    user_id = ForeignKeyField(
        schema="UserSchema",
        on_delete=ForeignKeyPolicy.CASCADE,
    )

    theme = EnumField(
        values=("system", "light", "dark"),
        default="system",
    )

    notifications = BooleanField(default=True)
