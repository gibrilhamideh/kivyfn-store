from kivyfn.store import SingleSchema
from kivyfn.store.schema.fields import NumericField, StringField


class UserSchema(SingleSchema):
    persistent = False

    age = NumericField(default=20)
    username = StringField(default="gibril")
