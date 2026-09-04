from __future__ import annotations

from collections.abc import Iterable
from threading import RLock
from typing import Any
from uuid import uuid4

from kivyfn.store.address import Address


class StoreRevisionTracker:
    """Track one volatile revision counter per store address."""

    __slots__ = (
        "_instance_id",
        "_lock",
        "_revisions",
    )

    def __init__(self) -> None:
        self._instance_id = uuid4().hex
        self._lock = RLock()
        self._revisions: dict[Address[Any], int] = {}

    def advance(
        self,
        address: Address[Any],
    ) -> int:
        with self._lock:
            revision = self._revisions.get(address, 0) + 1
            self._revisions[address] = revision
            return revision

    def revision(
        self,
        address: Address[Any],
    ) -> int:
        with self._lock:
            return self._revisions.get(address, 0)

    def report(
        self,
        addresses: Iterable[Address[Any]],
    ) -> dict[str, Any]:
        with self._lock:
            unique = dict.fromkeys(addresses)

            return {
                "server_instance_id": self._instance_id,
                "revisions": [
                    {
                        "address": address.to_list(),
                        "revision": self._revisions.get(address, 0),
                    }
                    for address in unique
                ],
            }

    def snapshot(
        self,
        address: Address[Any],
        data: Any,
    ) -> dict[str, Any]:
        with self._lock:
            return {
                "server_instance_id": self._instance_id,
                "revision": self._revisions.get(address, 0),
                "data": data,
            }

    @property
    def instance_id(self) -> str:
        with self._lock:
            return self._instance_id
