from __future__ import annotations
from typing import TYPE_CHECKING

from dataclasses import dataclass

if TYPE_CHECKING:
    from ..storage.protocol import StateStorage
    from ..builder.policy import BootPolicyMode


@dataclass(frozen=True)
class StoreConfig:
    storage: StateStorage | None = None
    boot_policy: BootPolicyMode | None = None
