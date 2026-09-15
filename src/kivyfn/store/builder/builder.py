from __future__ import annotations

from pathlib import Path
from typing import Literal

from kivyfn.store.builder.branch import Branch
from kivyfn.store.builder.config import HistoryConfig, HostConfig, StoreConfig
from kivyfn.store.builder.initializer import Initializer
from kivyfn.store.builder.migration import Migration
from kivyfn.store.builder.policy import BootPolicy


class Builder:
    __slots__ = (
        "_root_branch",
        "_storage_path",
        "_store_version",
        "_boot_policy",
        "_migrations",
        "_initializer",
        "_history",
        "_host",
        "_built",
    )

    def __init__(self) -> None:
        self._root_branch: Branch | None = None
        self._storage_path = Path("./storage")
        self._store_version = 1
        self._boot_policy = BootPolicy.RAISE
        self._migrations: list[Migration] = []
        self._initializer: Initializer | None = None
        self._history: HistoryConfig | None = None
        self._host: HostConfig | None = None
        self._built = False

    def tree(self, root: Branch) -> Builder:
        if self._root_branch is not None:
            raise RuntimeError("Tree already defined")
        self._root_branch = root
        return self

    def storage_path(self, path: str | Path) -> Builder:
        self._storage_path = Path(path)
        return self

    def store_version(self, version: int) -> Builder:
        self._store_version = version
        return self

    def boot_policy(
        self, policy: Literal["erase", "raise", "recover"]
    ) -> Builder:
        self._boot_policy = BootPolicy(policy)
        return self

    def migrations(self, migrations: list[Migration]) -> Builder:
        self._migrations = list(migrations)
        return self

    def initializer(self, initializer: Initializer | None) -> Builder:
        self._initializer = initializer
        return self

    def history(self, history: HistoryConfig | None) -> Builder:
        self._history = history
        return self

    def host(self, host: HostConfig | None) -> Builder:
        self._host = host
        return self

    def build(self) -> StoreConfig:
        if self._built:
            raise RuntimeError("Builder can only build once")
        if self._root_branch is None:
            raise RuntimeError("No tree defined. Call .tree() first.")

        config = StoreConfig(
            root=self._root_branch,
            storage_path=self._storage_path,
            store_version=self._store_version,
            boot_policy=self._boot_policy,
            migrations=self._migrations,
            initializer=self._initializer,
            history=self._history,
            host=self._host,
        )
        self._built = True
        return config
