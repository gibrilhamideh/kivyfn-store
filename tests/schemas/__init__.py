# tests/schemas/__init__.py
from pathlib import Path

from kivyfn.store.builder import Branch, Builder
from tests.schemas.root import RootSchema
from tests.schemas.settings import UserSettingsSchema
from tests.schemas.users import UserSchema

TEST_STORE_PATH = Path("~/Data/kivyfn-store/tests").expanduser()


def create_builder() -> Builder:
    root = (
        Branch(RootSchema)
        .child("users", Branch(UserSchema))
        .child("user-settings", Branch(UserSettingsSchema))
    )

    return (
        Builder()
        .tree(root)
        .storage_path(TEST_STORE_PATH)
        .boot_policy("recover")
    )
