from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kivyfn.store.builder.migration import Migration
from kivyfn.store.builder.policy import BootPolicy


@dataclass(frozen=True)
class StoreConfig:
    storage_path: Path = Path("./storage")

    store_version: int = 1

    boot_policy: BootPolicy = BootPolicy.RAISE

    migrations: list[Migration] = field(default_factory=list)
