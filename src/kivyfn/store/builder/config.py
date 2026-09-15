from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kivyfn.store.builder.branch import Branch
from kivyfn.store.builder.initializer import Initializer
from kivyfn.store.builder.migration import Migration
from kivyfn.store.builder.policy import BootPolicy

if TYPE_CHECKING:
    from kivyfn.store.devtools.config import DevToolsConfig


@dataclass(frozen=True, slots=True)
class HistoryConfig:
    """Fixed retention limits for one Store lifetime."""

    max_entries: int = 500
    max_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("max_entries", "max_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class HostConfig:
    """Optional hosting settings; the transport owns endpoint parsing."""

    endpoint: str
    devtools: bool | DevToolsConfig = False

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str):
            raise TypeError("endpoint must be a string")
        if type(self.devtools) is not bool:
            from kivyfn.store.devtools.config import DevToolsConfig

            if not isinstance(self.devtools, DevToolsConfig):
                raise TypeError("devtools must be bool or DevToolsConfig")


@dataclass(frozen=True)
class StoreConfig:
    """A detached tree definition and settings for a new Store instance.

    Each Store builds privately owned Nodes from ``root``. Hosting settings
    are consumed by StoreHost or StoreProcess; constructing a Store opens no
    listeners. Migration declarations retain their existing boot policy.
    """

    root: Branch
    storage_path: Path = Path("./storage")
    store_version: int = 1
    boot_policy: BootPolicy = BootPolicy.RAISE
    migrations: list[Migration] = field(default_factory=list)
    initializer: Initializer | None = None
    history: HistoryConfig | None = None
    host: HostConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.root, Branch):
            raise TypeError("root must be a Branch instance")
        _validate_tree(self.root, set())
        object.__setattr__(self, "root", deepcopy(self.root))
        object.__setattr__(self, "storage_path", Path(self.storage_path))
        object.__setattr__(self, "migrations", list(self.migrations))
        if type(self.store_version) is not int:
            raise TypeError("store_version must be an int")
        if self.store_version < 1:
            raise ValueError("store_version must be >= 1")
        if self.initializer is not None and not isinstance(
            self.initializer, Initializer
        ):
            raise TypeError("initializer must be Initializer or None")
        if self.history is not None and not isinstance(
            self.history, HistoryConfig
        ):
            raise TypeError("history must be HistoryConfig or None")
        if self.host is not None and not isinstance(self.host, HostConfig):
            raise TypeError("host must be HostConfig or None")


def _validate_tree(branch: Branch, ancestors: set[int]) -> None:
    branch_id = id(branch)
    if branch_id in ancestors:
        raise RuntimeError(
            "Cycle detected in Branch structure at schema "
            f"{branch.schema.__name__!r}"
        )
    ancestors.add(branch_id)
    for child in branch._raw_children().values():
        _validate_tree(child, ancestors)
    ancestors.remove(branch_id)
